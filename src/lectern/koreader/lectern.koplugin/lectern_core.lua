--[[ Lectern plugin logic that does not depend on KOReader's UI (tested without KOReader).

Core.new{
    http = transport,        -- lectern_http.lua (or a fake)
    json = { encode, decode },
    store = LuaSettings-like (readSetting, saveSetting, delSetting, flush),
    plugin_dir = path of lectern.koplugin,
    configure = function(config) end,  -- writes KOReader's kosync + OPDS settings
    logger = { info, warn },           -- optional
    now = os.time,                     -- optional
}

Secrets (the setup secret, userkey, app password, pairing code) are never passed to the
logger: every log line goes through Core:log, which also masks any known secret.
]]

local Core = {}
Core.__index = Core

Core.DEVICE_FILE = "lectern_device.lua"
Core.BOOTSTRAP_FILE = "lectern_bootstrap.txt"
Core.DEFAULT_PAGES = 10
Core.RETRY_DELAYS = { 30, 60, 120, 300, 900 } -- seconds, while the network is up
Core.OUTBOX_MAX = 50

Core.DEFAULT_OPTIONS = {
    auto_sync = true,                 -- pull on open, push on close and suspend
    pages_before_update = Core.DEFAULT_PAGES, -- also push every N page turns (0 = off)
    offline_mode = "queue",           -- "queue": wait for Wi-Fi; "wifi": turn Wi-Fi on
    sync_forward = "silent",          -- newer position elsewhere: "silent" jump or "prompt"
}

local KO_ACCEPT = "application/vnd.koreader.v1+json"

local function trim(s)
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

local function str(v)
    -- JSON nulls arrive as userdata (cjson.null, rapidjson.null).
    if type(v) == "string" then return v end
    if type(v) == "number" then return tostring(v) end
    return nil
end

local function b64(s)
    local ok, mime = pcall(require, "mime") -- LuaSocket, shipped with KOReader
    if ok and mime.b64 then return (mime.b64(s)) end
    return require("ffi/sha2").bin_to_base64(s)
end
Core._b64 = b64

function Core.new(deps)
    local self = setmetatable({}, Core)
    self.http = deps.http
    self.json = deps.json
    self.store = deps.store
    self.plugin_dir = deps.plugin_dir or "."
    self.configure = deps.configure or function() end
    self.logger = deps.logger
    self.now = deps.now or os.time
    return self
end

-- --- logging -----------------------------------------------------------------------------

function Core:secrets()
    local out = {}
    for _, k in ipairs({ "userkey", "app_password" }) do
        local v = self.store:readSetting(k)
        if type(v) == "string" and #v >= 6 then out[#out + 1] = v end
    end
    if self._bootstrap_secret then out[#out + 1] = self._bootstrap_secret end
    return out
end

function Core:redact(text)
    text = tostring(text)
    for _, s in ipairs(self:secrets()) do
        local i, j = text:find(s, 1, true)
        while i do
            text = text:sub(1, i - 1) .. "***" .. text:sub(j + 1)
            i, j = text:find(s, i + 3, true)
        end
    end
    -- Belt and braces: anything shaped like a setup secret.
    return (text:gsub("lb_[%w_%-]+", "lb_***"))
end

function Core:log(level, ...)
    if not self.logger then return end
    local parts = { "Lectern:" }
    for _, v in ipairs({ ... }) do parts[#parts + 1] = self:redact(v) end
    local fn = self.logger[level] or self.logger.info
    fn(table.concat(parts, " "))
end

-- --- files shipped in the zip -------------------------------------------------------------

function Core:devicePath() return self.plugin_dir .. "/" .. Core.DEVICE_FILE end
function Core:bootstrapPath() return self.plugin_dir .. "/" .. Core.BOOTSTRAP_FILE end

function Core:deviceFile()
    local chunk = loadfile(self:devicePath())
    if not chunk then return nil end
    setfenv(chunk, {})
    local ok, t = pcall(chunk)
    if ok and type(t) == "table" then return t end
    return nil
end

function Core:bootstrapSecret()
    local f = io.open(self:bootstrapPath(), "rb")
    if not f then return nil end
    local s = f:read("*a") or ""
    f:close()
    s = trim(s)
    if s == "" then return nil end
    return s
end

function Core:deleteBootstrap()
    os.remove(self:bootstrapPath())
    self._bootstrap_secret = nil
    return io.open(self:bootstrapPath(), "rb") == nil
end

-- --- settings ------------------------------------------------------------------------------

function Core:options()
    local o = self.store:readSetting("options") or {}
    for k, v in pairs(Core.DEFAULT_OPTIONS) do
        if o[k] == nil then o[k] = v end
    end
    return o
end

function Core:setOption(key, value)
    local o = self:options()
    o[key] = value
    self.store:saveSetting("options", o)
    self.store:flush()
    if self:isPaired() then self.configure(self:config()) end
end

function Core:isPaired()
    return self.store:readSetting("userkey") ~= nil and self.store:readSetting("app_password") ~= nil
end

--- Where the server is: what pairing stored, else what the zip carried.
function Core:serverUrl()
    local s = self.store:readSetting("server_url")
    if s then return s end
    local d = self:deviceFile()
    return d and d.server_url
end

function Core:username()
    local u = self.store:readSetting("username")
    if u then return u end
    local d = self:deviceFile()
    return d and d.username
end

--- Everything main.lua needs to write KOReader's kosync and OPDS settings.
function Core:config()
    return {
        server_url = self.store:readSetting("server_url"),
        username = self.store:readSetting("username"),
        userkey = self.store:readSetting("userkey"),
        app_password = self.store:readSetting("app_password"),
        label = self.store:readSetting("label"),
        device_id = self.store:readSetting("device_id"),
        options = self:options(),
    }
end

function Core:applyCredentials(payload, server_url)
    local userkey, password = str(payload.userkey), str(payload.app_password)
    local username = str(payload.username)
    if not userkey or not password or not username then
        return false
    end
    local s = self.store
    s:saveSetting("server_url", server_url)
    s:saveSetting("username", username)
    s:saveSetting("userkey", userkey)
    s:saveSetting("app_password", password)
    s:saveSetting("device_id", str(payload.device_id))
    s:saveSetting("label", str(payload.label))
    s:saveSetting("paired_at", self.now())
    s:saveSetting("last_ok_at", self.now())
    s:delSetting("last_error")
    s:flush()
    self.configure(self:config())
    return true
end

-- --- HTTP ----------------------------------------------------------------------------------

function Core:request(method, path, opts)
    opts = opts or {}
    local base = opts.server_url or self:serverUrl()
    if not base then return nil, nil, "no server" end
    local headers = { accept = opts.accept or "application/json" }
    local body
    if opts.json then
        body = self.json.encode(opts.json)
        headers["content-type"] = "application/json"
    end
    if opts.basic then
        headers["authorization"] = "Basic " .. b64(self.store:readSetting("username") .. ":"
            .. self.store:readSetting("app_password"))
    end
    if opts.kosync then
        headers["x-auth-user"] = self.store:readSetting("username")
        headers["x-auth-key"] = self.store:readSetting("userkey")
    end
    local code, text, err = self.http{
        method = method, url = base .. path, headers = headers, body = body,
        sink = opts.sink, timeout = opts.timeout,
    }
    if not code then
        return nil, nil, err or "offline"
    end
    local data
    if text and text ~= "" and not opts.sink then
        local ok, decoded = pcall(self.json.decode, text)
        if ok then data = decoded end
    end
    return code, data
end

function Core:noteResult(code, err)
    if code and code < 500 and code ~= 401 then
        self.store:saveSetting("last_ok_at", self.now())
        self.store:delSetting("last_error")
    elseif code == 401 then
        self.store:saveSetting("last_error", "unauthorized")
    else
        self.store:saveSetting("last_error", code and ("server " .. code) or "offline")
    end
    self.store:saveSetting("last_attempt_at", self.now())
    self.store:flush()
end

--- First start: trade the zip's setup secret for this device's credentials.
-- Returns "ok", "none" (no secret on disk), "invalid" (used, expired, or revoked: the secret
-- is deleted and Pair is the way forward), or "retry" (offline, rate limited, server error).
function Core:bootstrap()
    local secret = self:bootstrapSecret()
    if not secret then return "none" end
    self._bootstrap_secret = secret
    local server = self:serverUrl()
    if not server then
        self:log("warn", "setup secret present but no server address in", Core.DEVICE_FILE)
        return "retry"
    end
    local code, data, err = self:request("POST", "/koplugin/bootstrap", {
        server_url = server, json = { secret = secret }, timeout = { 5, 15 },
    })
    if code == 200 and type(data) == "table" then
        if self:applyCredentials(data, str(data.server_url) or server) then
            local gone = self:deleteBootstrap()
            self:log("info", "set up as device", str(data.label) or "?", gone and "" or "(could not delete setup file)")
            return "ok"
        end
        return "retry"
    end
    if code == 410 or code == 400 or code == 401 or code == 422 then
        self:deleteBootstrap()
        self:log("warn", "setup secret refused by the server (HTTP " .. code .. "); pair with a code instead")
        return "invalid"
    end
    self:log("info", "setup postponed:", code and ("HTTP " .. code) or (err or "offline"))
    return "retry"
end

--- Lectern → Pair: a 6-digit code from the web app. Returns "ok", "invalid", or "retry".
function Core:pair(server_url, username, code)
    local code_status, data, err = self:request("POST", "/koplugin/pair", {
        server_url = server_url, json = { username = username, code = code }, timeout = { 5, 15 },
    })
    if code_status == 200 and type(data) == "table" then
        if self:applyCredentials(data, server_url) then
            self:deleteBootstrap() -- a stale zip secret is superseded
            self:log("info", "paired as device", str(data.label) or "?")
            return "ok"
        end
    end
    if code_status == 401 or code_status == 422 or code_status == 400 then
        return "invalid"
    end
    if code_status == 429 then return "rate_limited" end
    self:log("info", "pairing failed:", code_status and ("HTTP " .. code_status) or (err or "offline"))
    return "retry"
end

--- GET /koplugin/status: device, queue. Returns the table, or nil and a reason.
function Core:status()
    if not self:isPaired() then return nil, "not_paired" end
    local code, data, err = self:request("GET", "/koplugin/status", { basic = true, timeout = { 4, 10 } })
    self:noteResult(code, err)
    if code == 200 and type(data) == "table" then
        self.store:saveSetting("queue_pending", tonumber(data.queue and data.queue.pending) or 0)
        self.store:flush()
        return data
    end
    if code == 401 then return nil, "unauthorized" end
    return nil, code and ("server " .. code) or "offline"
end

-- --- outbox: progress waiting for Wi-Fi or Tailscale ---------------------------------------

function Core:outbox()
    return self.store:readSetting("outbox") or {}
end

--- Remember the latest position of a document (one entry per document, newest wins).
function Core:queuePush(item)
    if not item or not item.document then return end
    local box = self:outbox()
    for i = #box, 1, -1 do
        if box[i].document == item.document then table.remove(box, i) end
    end
    item.queued_at = self.now()
    box[#box + 1] = item
    while #box > Core.OUTBOX_MAX do table.remove(box, 1) end
    self.store:saveSetting("outbox", box)
    self.store:flush()
end

--- Send what is waiting, oldest first. Stops at the first failure and keeps the rest.
-- Returns sent count, remaining count, and nil or the reason it stopped.
function Core:drain()
    local box = self:outbox()
    if #box == 0 or not self:isPaired() then return 0, #box end
    local sent = 0
    while #box > 0 do
        local item = box[1]
        local code, _, err = self:request("PUT", "/syncs/progress", {
            kosync = true, accept = KO_ACCEPT, timeout = { 3, 8 },
            json = {
                document = item.document, progress = item.progress, percentage = item.percentage,
                device = item.device or "KOReader", device_id = item.device_id or "",
            },
        })
        self:noteResult(code, err)
        if code == 200 then
            table.remove(box, 1)
            sent = sent + 1
        elseif code == 400 or code == 422 then
            -- The server read this item and will never accept it; do not block the rest.
            -- Anything else (403, 404, 405 from a wrong address, 401, 429, 5xx) keeps the
            -- outbox intact: the positions are still good once the address is right.
            table.remove(box, 1)
        else
            self.store:saveSetting("outbox", box)
            self.store:flush()
            return sent, #box, code == 401 and "unauthorized" or (code and ("server " .. code) or (err or "offline"))
        end
    end
    self.store:saveSetting("outbox", box)
    self.store:flush()
    if sent > 0 then
        self.store:saveSetting("last_push_at", self.now())
        self.store:flush()
    end
    return sent, 0
end

function Core:retryDelay(attempt)
    local d = Core.RETRY_DELAYS
    return d[math.min(math.max(attempt, 1), #d)]
end

-- --- books sent to this device -------------------------------------------------------------

local function safe_name(s)
    s = (s or "book"):gsub("[%c/\\:%*%?\"<>|]", "_"):gsub("^%s+", ""):gsub("%s+$", "")
    if s == "" or s:match("^%.+$") then s = "book" end
    return s:sub(1, 120)
end
Core._safe_name = safe_name

local EXT = {
    ["application/epub+zip"] = ".epub",
    ["application/pdf"] = ".pdf",
    ["application/vnd.comicbook+zip"] = ".cbz",
}

--- Download one queued book into ``dir``. Returns the path, or nil and a reason.
-- A complete download is what moves the book from "Queued" to "Delivered" on the server.
function Core:download(item, dir)
    local file = item.files and item.files[1]
    if not file then return nil, "no_file" end
    local ext = EXT[file.media_type] or (str(file.name) or ""):match("%.%w+$") or ""
    local base = safe_name(item.author and item.author ~= "" and (item.title .. " - " .. item.author) or item.title)
    local path = dir .. "/" .. base .. ext
    local n = 1
    local function exists(p)
        local f = io.open(p, "rb")
        if f then f:close() return true end
        return false
    end
    -- Never replace a book already there (side-loaded, or annotated by the reader).
    while exists(path) do
        n = n + 1
        path = string.format("%s/%s (%d)%s", dir, base, n, ext)
    end
    local tmp = path .. ".part"
    local out = io.open(tmp, "wb")
    if not out then return nil, "cannot_write" end
    local ltn12 = require("ltn12")
    local code, _, err = self:request("GET", file.download_url, {
        basic = true, accept = "*/*", sink = ltn12.sink.file(out), timeout = { 10, 600 },
    })
    self:noteResult(code, err)
    if code ~= 200 then
        os.remove(tmp)
        return nil, code and ("server " .. code) or (err or "offline")
    end
    local ok = os.rename(tmp, path)
    if not ok then os.remove(tmp) return nil, "cannot_write" end
    return path
end

--- One line for the menu: what state the connection is in.
function Core:statusLine(online, ago)
    local box = #self:outbox()
    if not self:isPaired() then
        if self:bootstrapSecret() then return "Setting up: waiting for the network" end
        return "Not paired"
    end
    local err = self.store:readSetting("last_error")
    if err == "unauthorized" then return "Device revoked or replaced: pair again" end
    if box > 0 then
        return string.format("%s · %d %s waiting to send", online and "Retrying" or "Offline", box,
            box == 1 and "position" or "positions")
    end
    if err and err ~= "" then return "Not reachable (" .. err .. ")" end
    local last = self.store:readSetting("last_ok_at")
    if last and ago then return "Connected · last contact " .. ago(last) end
    return "Connected"
end

return Core
