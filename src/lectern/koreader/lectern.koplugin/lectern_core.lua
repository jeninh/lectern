--[[ Lectern plugin logic that does not depend on KOReader's UI (tested without KOReader).

Core.new{
    http = transport,        -- lectern_http.lua (or a fake)
    json = { encode, decode },
    store = LuaSettings-like (readSetting, saveSetting, delSetting, flush),
    plugin_dir = path of lectern.koplugin,
    configure = function(config) end,  -- writes KOReader's kosync + OPDS settings
    logger = { info, warn },           -- optional
    now = os.time,                     -- optional
    sha2 = KOReader's ffi/sha2,        -- optional (sha256, hmac): the home address check
}

Two addresses: server_url (Tailscale) and, with home Wi-Fi access on, lan_url
(docs/home-network.md). Core:route() prefers the home address, but only after the server there
proved it is this device's Lectern (POST /koplugin/hello: an HMAC of a fresh nonce keyed by the
SHA-256 of a secret this device holds), so no credential goes to another network's machine
that happens to have the same private IP.

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
Core.ROUTE_TTL = 600 -- seconds a home/Tailscale choice is trusted before it is checked again

--- Looks like a home-network address (private IPv4, IPv6 link-local or unique-local, or an
-- mDNS .local name). With home Wi-Fi access on, such an address is never the Tailscale
-- fallback: the fallback is used without a check, and another network may have a different
-- machine at the same private address.
function Core.homeLike(url)
    if type(url) ~= "string" then return false end
    local auth = url:lower():match("^%a+://([^/?#]*)")
    if not auth or auth:find("@", 1, true) then return true end -- unparseable: not trusted
    -- Any IPv6 literal (mapped IPv4, link-local, unique-local; Tailscale's own are ULA too).
    if auth:sub(1, 1) == "[" then return true end
    -- Only a plain host name or dotted IPv4 with an optional numeric port is classified;
    -- anything else (a named port such as :http-alt, unbracketed IPv6, odd characters) is
    -- something the socket library may still resolve, so it is never trusted.
    local host = auth:gsub(":%d+$", ""):gsub("%.+$", "") -- port, DNS-root dots
    if not host:match("^[%w%-%.]+$") then return true end
    if host == "local" or host:match("%.local$") then return true end
    local numeric = host:match("^[%d.]+$") or host:match("%.%d+$") or host:match("^0x")
        or host:match("%.0x")
    if not numeric then return false end
    -- Numeric hosts must be canonical dotted quads; anything else (10.1, 0x0a.1, 010.0.0.1)
    -- resolves to some IPv4 address in the socket library, so it is never trusted.
    local q = { host:match("^(%d+)%.(%d+)%.(%d+)%.(%d+)$") }
    if #q ~= 4 then return true end
    for k = 1, 4 do
        if (#q[k] > 1 and q[k]:sub(1, 1) == "0") or tonumber(q[k]) > 255 then return true end
        q[k] = tonumber(q[k])
    end
    local a, b = q[1], q[2]
    return a == 10 or (a == 172 and b >= 16 and b <= 31) or (a == 192 and b == 168)
        or (a == 169 and b == 254)
end

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
    self.sha2 = deps.sha2
    if not self.sha2 then
        local ok, sha2 = pcall(require, "ffi/sha2")
        if ok then self.sha2 = sha2 end
    end
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

--- The home Wi-Fi address, if Lectern offers one: what the server last said, else the zip's.
function Core:lanUrl()
    local s = self.store:readSetting("lan_url")
    if s then return s ~= "" and s or nil end
    local d = self:deviceFile()
    return d and d.lan_url
end

--- Once a home address was ever known, a home-like Tailscale fallback stays untrusted for good,
-- even after the server stops announcing one (it could have been the home address itself).
function Core:homeSeen()
    return self.store:readSetting("lan_seen") == true or self:lanUrl() ~= nil
end

function Core:saveLanUrl(value)
    if self:lanUrl() or str(value) then self.store:saveSetting("lan_seen", true) end
    -- "" remembers "the server has none" (so the zip's address is not used again).
    self.store:saveSetting("lan_url", str(value) or "")
    self.store:delSetting("active_url")
    self._route, self._route_at = nil, nil
end

--- Stop trusting the home address until it proves itself again: on start, wake, and network
-- changes (another Wi-Fi may have a different machine at the same private IP). KOReader's own
-- sync and catalogue settings go back to the Tailscale address at once, synchronously.
function Core:forgetHome()
    self._route, self._route_at = nil, nil
    local active = self.store:readSetting("active_url")
    local stale = active and active ~= ""
    if stale then
        self.store:delSetting("active_url")
        self.store:flush()
    end
    if self:isPaired() and (stale or (self:lanUrl() and not self:fallbackUrl())) then
        self.configure(self:config())
    end
end

local function nonce()
    local f = io.open("/dev/urandom", "rb")
    local bytes = f and f:read(16)
    if f then f:close() end
    if not bytes or #bytes < 16 then
        local t = {}
        for i = 1, 16 do t[i] = string.char(math.random(0, 255)) end
        bytes = table.concat(t)
    end
    return (bytes:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

--- The secret this device holds and the server keeps a SHA-256 of: the app password once
-- paired, before that the setup secret from the zip.
function Core:proofSecret()
    if self:isPaired() then
        return self.store:readSetting("app_password"), self.store:readSetting("device_id"), false
    end
    local d = self:deviceFile()
    return self:bootstrapSecret(), d and d.device_id, true
end

--- Is the Lectern at ``url`` this device's? Sends nothing secret: a nonce, checked against
-- the server's HMAC of it.
function Core:verifyHome(url)
    local secret, device_id, setup = self:proofSecret()
    if not secret or not device_id or not self.sha2 then return false end
    local n = nonce()
    local code, data = self:request("POST", "/koplugin/hello", {
        server_url = url, json = { device_id = device_id, nonce = n, setup = setup },
        timeout = { 2, 4 },
    })
    if code ~= 200 or type(data) ~= "table" then return false end
    local sha2 = self.sha2
    return str(data.proof) == sha2.hmac(sha2.sha256, sha2.sha256(secret), "lectern-lan:" .. n)
end

--- Where to send requests now: the home address when it answers as this Lectern, else the
-- Tailscale one. Checked again after ROUTE_TTL, or at once with ``force`` (network change,
-- Sync now, a failed request). A change rewrites KOReader's kosync and OPDS settings.
-- The home address is never used unverified, and never as the fallback: a fallback that is a
-- home address (only possible before setup, from a zip) gives no route until the check passes.
function Core:route(force)
    local remote, lan = self:fallbackUrl(), self:lanUrl()
    if not lan then return remote end
    local now = self.now()
    if not force and self._route_at and now - self._route_at < Core.ROUTE_TTL then
        return self._route
    end
    local home = self:verifyHome(lan)
    local pick = home and lan or remote
    self._route, self._route_at = pick, pick and now or nil
    local want = home and lan or ""
    if self:isPaired() and (want ~= (self.store:readSetting("active_url") or "") or not pick) then
        self.store:saveSetting("active_url", want)
        self.store:flush()
        self:log("info", home and "at home: using the home Wi-Fi address"
            or (pick and "not at home: using Tailscale" or "no trusted address: sync paused"))
        self.configure(self:config()) -- without a trusted address, this removes the credentials
    end
    return pick
end

--- The Tailscale address, unless it is home-like while home Wi-Fi access is on (then there is
-- no fallback that may be used without a check).
function Core:fallbackUrl()
    local remote, lan = self:serverUrl(), self:lanUrl()
    if remote and (remote == lan or (self:homeSeen() and Core.homeLike(remote))) then return nil end
    return remote
end

--- The address in use: the home one while verified, else the fallback (nil: none trusted).
function Core:activeUrl()
    local lan = self:lanUrl()
    if lan and self.store:readSetting("active_url") == lan then return lan end
    return self:fallbackUrl()
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
        server_url = self:activeUrl(),
        -- KOReader's OPDS browser keeps copies of its links and credentials while it is open,
        -- out of the plugin's reach, so the catalogue entry always uses the Tailscale address.
        opds_url = self:fallbackUrl(),
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
    local lan = str(payload.lan_url) or self:lanUrl()
    if server_url == lan or ((lan or self:homeSeen()) and Core.homeLike(server_url)) then
        self:log("warn", "refused a home-network address as the Tailscale address")
        return false
    end
    local s = self.store
    s:saveSetting("server_url", server_url)
    if payload.lan_url ~= nil then self:saveLanUrl(payload.lan_url) end
    s:delSetting("active_url")
    self._route, self._route_at = nil, nil
    s:saveSetting("username", username)
    s:saveSetting("userkey", userkey)
    s:saveSetting("app_password", password)
    s:saveSetting("device_id", str(payload.device_id))
    s:saveSetting("label", str(payload.label))
    s:saveSetting("paired_at", self.now())
    s:saveSetting("last_ok_at", self.now())
    s:delSetting("last_error")
    s:delSetting("setup_error")
    s:flush()
    self.configure(self:config()) -- Tailscale first; the home address only once verified
    if self:lanUrl() then self:route(true) end
    return true
end

-- --- HTTP ----------------------------------------------------------------------------------

function Core:request(method, path, opts)
    opts = opts or {}
    local base = opts.server_url or self:route()
    if not base then return nil, nil, "no server" end
    local code, data, err = self:send(base, method, path, opts)
    if not code and not opts.server_url and self._route then
        -- Left home (or came back) since the last check: try the other address once.
        local again = self:route(true)
        if again and again ~= base then code, data, err = self:send(again, method, path, opts) end
    end
    return code, data, err
end

function Core:send(base, method, path, opts)
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
-- is deleted and Pair is the way forward), or "retry" and the reason (connection error, rate
-- limited, server error). The request is always attempted: whether the network is up is
-- decided by the request itself, not by KOReader's NetworkMgr, which stays "offline" on
-- devices whose Wi-Fi is managed outside KOReader.
function Core:bootstrap()
    local secret = self:bootstrapSecret()
    if not secret then return "none" end
    self._bootstrap_secret = secret
    local function postpone(why)
        self:log("info", "setup postponed:", why)
        self.store:saveSetting("setup_error", why)
        self.store:saveSetting("last_attempt_at", self.now())
        self.store:flush()
        return "retry", why
    end
    local server = self:serverUrl()
    if not server then
        self:log("warn", "setup secret present but no server address in", Core.DEVICE_FILE)
        return postpone("no server address")
    end
    local code, data, err = self:request("POST", "/koplugin/bootstrap", {
        server_url = self:route(true), json = { secret = secret }, timeout = { 3, 10 },
    })
    if code == 200 and type(data) == "table" then
        if self:applyCredentials(data, str(data.server_url) or server) then
            local gone = self:deleteBootstrap()
            self:log("info", "set up as device", str(data.label) or "?", gone and "" or "(could not delete setup file)")
            return "ok"
        end
        return postpone("unexpected answer")
    end
    if code == 410 or code == 400 or code == 401 or code == 422 then
        self:deleteBootstrap()
        self:log("warn", "setup secret refused by the server (HTTP " .. code .. "); pair with a code instead")
        return "invalid"
    end
    return postpone(code and ("HTTP " .. code) or (err or "offline"))
end

--- ``scheme://host[:port]`` with a plain host name, dotted IPv4, or bracketed IPv6 and an
-- optional numeric port; nothing else is accepted as a server address.
function Core.validServerUrl(url)
    if type(url) ~= "string" then return false end
    local host = url:match("^https?://([%w%-%.]+)$") or url:match("^https?://([%w%-%.]+):%d+$")
    if host then return true end
    return url:match("^https?://%[[%x:%.]+%]:%d+$") ~= nil or url:match("^https?://%[[%x:%.]+%]$") ~= nil
end

--- Lectern → Pair: a 6-digit code from the web app. Returns "ok", "invalid", or "retry".
function Core:pair(server_url, username, code)
    if not Core.validServerUrl(server_url) then return "invalid_server" end
    local code_status, data, err = self:request("POST", "/koplugin/pair", {
        server_url = server_url, json = { username = username, code = code }, timeout = { 5, 15 },
    })
    if code_status == 200 and type(data) == "table" then
        -- The Tailscale fallback: the server's own Tailscale address, else the address entered,
        -- else the one known before; never a home-network address (it is used without a check).
        local lan = str(data.lan_url) or self:lanUrl()
        local seen = lan or self:homeSeen()
        local remote
        for _, candidate in ipairs({ str(data.server_url) or false, server_url, self:serverUrl() or false }) do
            if candidate and candidate ~= lan and not (seen and Core.homeLike(candidate)) then
                remote = candidate
                break
            end
        end
        if not remote then
            self:log("warn", "paired over the home address, but no Tailscale address is known")
            return "no_fallback"
        end
        if self:applyCredentials(data, remote) then
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
        if data.lan_url ~= nil and str(data.lan_url) ~= self:lanUrl() then
            self:saveLanUrl(data.lan_url) -- turned on, off, or moved on the server
            self.store:flush()
            self.configure(self:config()) -- Tailscale until the next request checks again
        end
        self.store:saveSetting("queue_pending", tonumber(data.queue and data.queue.pending) or 0)
        self.store:flush()
        return data
    end
    if code == 401 then return nil, "unauthorized" end
    return nil, code and ("server " .. code) or (err or "offline")
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
        if self:bootstrapSecret() then
            local why = self.store:readSetting("setup_error")
            if why then return "Setting up: will retry (" .. why .. ")" end
            return "Setting up: waiting for the network"
        end
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
