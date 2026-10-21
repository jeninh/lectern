--[[ Runs lectern.koplugin outside KOReader, against a real Lectern server, for
tests/test_koplugin.py. KOReader's modules are replaced by small fakes that behave like the
real ones where the plugin depends on them (LuaSettings files, readSetting defaults, plugin
instances that cache their settings, NetworkMgr's online state, UIManager's scheduler).

usage: luajit harness.lua <plugin_dir> <settings_dir> <command> [args...]
Prints one JSON object on stdout.
]]

local plugin_dir, settings_dir, command = arg[1], arg[2], arg[3]
local here = debug.getinfo(1, "S").source:sub(2):match("(.*/)") or "./"
package.path = plugin_dir .. "/?.lua;" .. here .. "?.lua;" .. package.path

local cjson = require("cjson")
package.preload["json"] = function() return cjson end
package.preload["rapidjson"] = function() error("not in tests") end

-- --- fakes ---------------------------------------------------------------------------------

local function serialize(v, indent)
    indent = indent or ""
    local t = type(v)
    if t == "string" then return string.format("%q", v) end
    if t == "number" or t == "boolean" then return tostring(v) end
    if t == "table" then
        local keys = {}
        for k in pairs(v) do keys[#keys + 1] = k end
        table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
        local out = { "{\n" }
        for _, k in ipairs(keys) do
            local key = type(k) == "number" and ("[" .. k .. "]") or ("[" .. string.format("%q", k) .. "]")
            out[#out + 1] = indent .. "  " .. key .. " = " .. serialize(v[k], indent .. "  ") .. ",\n"
        end
        out[#out + 1] = indent .. "}"
        return table.concat(out)
    end
    return "nil"
end

local LuaSettings = {}
LuaSettings.__index = LuaSettings
function LuaSettings:open(path)
    local o = setmetatable({ file = path, data = {} }, LuaSettings)
    local chunk = loadfile(path)
    if chunk then
        local ok, t = pcall(chunk)
        if ok and type(t) == "table" then o.data = t end
    end
    return o
end
function LuaSettings:readSetting(k, default)
    if self.data[k] == nil and default ~= nil then self.data[k] = default end
    return self.data[k]
end
function LuaSettings:saveSetting(k, v) self.data[k] = v return self end
function LuaSettings:delSetting(k) self.data[k] = nil return self end
function LuaSettings:has(k) return self.data[k] ~= nil end
function LuaSettings:isTrue(k) return self.data[k] == true end
function LuaSettings:flush()
    if not self.file then return end
    local f = assert(io.open(self.file, "w"))
    f:write("return " .. serialize(self.data) .. "\n")
    f:close()
end
package.preload["luasettings"] = function() return LuaSettings end

G_reader_settings = LuaSettings:open(settings_dir .. "/settings.reader.lua")

package.preload["datastorage"] = function()
    return {
        getSettingsDir = function() return settings_dir end,
        getDataDir = function() return settings_dir end,
    }
end

package.preload["device"] = function()
    return { model = "Kindle", home_dir = settings_dir .. "/books", hasWifiToggle = function() return true end }
end

local NET = { online = true, connected = true, prompts = 0 }
package.preload["ui/network/manager"] = function()
    return {
        isOnline = function() return NET.online end,
        isConnected = function() return NET.connected end,
        willRerunWhenOnline = function(_, cb)
            if NET.online then return false end
            NET.prompts = NET.prompts + 1
            return true
        end,
        runWhenOnline = function(_, cb)
            NET.prompts = NET.prompts + 1
            if NET.online then cb() end
        end,
    }
end

local UI = { shown = {}, tasks = {} }
package.preload["ui/uimanager"] = function()
    return {
        show = function(_, w) UI.shown[#UI.shown + 1] = w end,
        close = function() end,
        forceRePaint = function() end,
        nextTick = function(_, fn) fn() end,
        scheduleIn = function(_, s, fn) UI.tasks[#UI.tasks + 1] = { s = s, fn = fn } end,
        unschedule = function(_, fn)
            for i = #UI.tasks, 1, -1 do
                if UI.tasks[i].fn == fn then table.remove(UI.tasks, i) end
            end
        end,
    }
end

local function widget()
    local W = {}
    function W:new(o) o = o or {} setmetatable(o, { __index = self }) return o end
    function W:extend(o) o = o or {} setmetatable(o, { __index = self }) return o end
    return W
end
for _, m in ipairs({ "ui/widget/confirmbox", "ui/widget/infomessage", "ui/widget/menu",
    "ui/widget/spinwidget", "ui/widget/multiinputdialog" }) do
    package.preload[m] = widget
end
package.preload["ui/widget/container/widgetcontainer"] = function()
    local WC = {}
    function WC:extend(sub)
        sub = sub or {}
        setmetatable(sub, { __index = self })
        return sub
    end
    function WC:new(o)
        o = o or {}
        setmetatable(o, { __index = self })
        if o.init then o:init() end
        return o
    end
    return WC
end

local LOG = {}
package.preload["logger"] = function()
    local function rec(...)
        local parts = {}
        for _, v in ipairs({ ... }) do parts[#parts + 1] = tostring(v) end
        LOG[#LOG + 1] = table.concat(parts, " ")
    end
    return { info = rec, warn = rec, err = rec, dbg = rec }
end
package.preload["gettext"] = function() return function(s) return s end end

-- kosync and OPDS as KOReader loads them: the instance caches its settings table (kosync in
-- a class-level LuaSettings object), so the plugin must edit those, not just the files.
local function fake_kosync()
    local obj = LuaSettings:open(settings_dir .. "/kosync.lua")
    local k = {
        settings_obj = obj,
        settings = obj:readSetting("settings", { auto_sync = false, sync_forward = 1, sync_backward = 3 }),
        device_id = "dev-123",
        calls = {},
    }
    function k:updateProgress(ensure, interactive) self.calls[#self.calls + 1] = { "push", ensure, interactive } end
    function k:getProgress(ensure, interactive) self.calls[#self.calls + 1] = { "pull", ensure, interactive } end
    function k:getDocumentDigest() return "0123456789abcdef0123456789abcdef" end
    function k:getLastProgress() return "/body/DocFragment[2]/body/p[3]/text().0" end
    function k:getLastPercent() return 0.42 end
    function k:registerEvents() self.events_registered = true end
    return k
end

local function fake_opds()
    local o = {}
    function o:loadSettings()
        if self.settings then return end
        self.settings = LuaSettings:open(settings_dir .. "/opds.lua")
        self.servers = self.settings:readSetting("servers", { { title = "Project Gutenberg", url = "https://m.gutenberg.org/ebooks.opds/?format=opds" } })
    end
    return o
end

-- --- run -----------------------------------------------------------------------------------

local Lectern = dofile(plugin_dir .. "/main.lua")
local function make(reader)
    local ui = { menu = { registerToMainMenu = function(_, p) local items = {} p:addToMainMenu(items) UI.menu = items end } }
    ui.opds = fake_opds()
    if reader then
        ui.document = {}
        ui.kosync = fake_kosync()
    end
    return Lectern:new{ ui = ui, path = plugin_dir }, ui
end

local function run_tasks()
    local tasks = UI.tasks
    UI.tasks = {}
    for _, t in ipairs(tasks) do t.fn() end
    return #tasks
end

local function file_exists(p) local f = io.open(p) if f then f:close() return true end return false end
local function read_settings(name) local s = LuaSettings:open(settings_dir .. "/" .. name) return s.data end

local out = {}
if command == "setup" then
    NET.online = arg[4] ~= "offline"
    NET.connected = NET.online
    local p, ui = make(false)
    out.pending = p.setup_pending == true
    out.paired = p.core:isPaired()
    out.secret_on_disk = file_exists(plugin_dir .. "/lectern_bootstrap.txt")
    out.kosync = read_settings("kosync.lua").settings
    out.opds = ui.opds.servers
    out.messages = #UI.shown
    out.status_line = p.core:statusLine(NET.online)
    out.menu = UI.menu and UI.menu.lectern and #UI.menu.lectern.sub_item_table or 0
elseif command == "offline-push" then
    NET.online, NET.connected = false, false
    local p, ui = make(true)
    local k = ui.kosync
    out.auto_sync = k.settings.auto_sync
    out.pages_before_update = k.settings.pages_before_update
    -- What kosync does on suspend and when a book opens, with automatic sync on.
    k:updateProgress(true, false, true)
    k:getProgress(true, false)
    out.kosync_calls = #k.calls
    out.outbox = #p.core:outbox()
    out.prompts = NET.prompts
    out.messages = #UI.shown
    out.tasks = #UI.tasks
    out.status_line = p.core:statusLine(false)
elseif command == "reconnect" then
    NET.online, NET.connected = true, true
    local p = make(false)
    p:onNetworkConnected()
    out.scheduled = #UI.tasks
    out.ran = run_tasks()
    out.outbox = #p.core:outbox()
    out.messages = #UI.shown
elseif command == "server-down" then
    -- Wi-Fi is up but the server (Tailscale) is not reachable: keep, retry later, quietly.
    NET.online, NET.connected = true, true
    local p, ui = make(true)
    local store = p.store
    store:saveSetting("server_url", arg[4])
    ui.kosync:updateProgress(false, false)
    out.outbox = #p.core:outbox()
    out.retry_in = UI.tasks[1] and UI.tasks[1].s
    run_tasks()
    out.retry_in_2 = UI.tasks[1] and UI.tasks[1].s
    out.messages = #UI.shown
    out.status_line = p.core:statusLine(true)
elseif command == "setup-retry" then
    -- Wi-Fi is up, the server is not reachable yet (Tailscale still connecting).
    NET.online, NET.connected = true, true
    local p = make(false)
    out.pending = p.setup_pending == true
    local delays = {}
    for _ = 1, 3 do
        delays[#delays + 1] = UI.tasks[1] and UI.tasks[1].s or -1
        run_tasks()
    end
    out.delays = delays
    p:onNetworkConnected()
    out.after_reconnect = UI.tasks[1] and UI.tasks[1].s
    out.messages = #UI.shown
elseif command == "status" then
    local p = make(false)
    local status, err = p.core:status()
    out.status = status
    out.err = err
    if status and status.queue.items[1] then
        os.execute("mkdir -p '" .. settings_dir .. "/books'")
        out.downloaded = p.core:download(status.queue.items[1], settings_dir .. "/books")
    end
elseif command == "pair" then
    local p = make(false)
    p:doPair(arg[4], arg[5], arg[6])
    out.message = UI.shown[1] and UI.shown[1].text
    out.paired = p.core:isPaired()
    out.kosync = read_settings("kosync.lua").settings
elseif command == "sync-now" then
    local p, ui = make(true)
    p:syncNow()
    out.message = UI.shown[1] and UI.shown[1].text
    out.outbox = #p.core:outbox()
end
out.log = LOG
out.settings = read_settings("lectern.lua")
io.write(cjson.encode(out))
