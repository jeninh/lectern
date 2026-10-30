--[[ Lectern for KOReader (docs/koreader-plugin.md).

First start: trades the one-time setup secret in lectern_bootstrap.txt for this device's own
credentials, writes KOReader's Progress sync (kosync) server, username and key and an OPDS
catalogue entry, and deletes the secret. Without network it retries quietly with backoff.

With home Wi-Fi access on (docs/home-network.md) it knows two addresses and uses the home one
whenever the Lectern there proves it is this device's server, Tailscale otherwise.

Then: automatic progress sync through KOReader's own kosync plugin (pull on open, push on
close and suspend, and every N page turns), with pushes that cannot reach the server kept in
an outbox and retried quietly once Wi-Fi (and Tailscale) is back, instead of prompting to turn
Wi-Fi on. Menu: Sync now, Books sent to this device, connection status, settings, Pair.

Highlights and notes go to Lectern the same way (docs/koreader-plugin.md#highlights-and-notes):
the open book's whenever its position is pushed and shortly after one is made, edited, or
deleted; Sync now also checks the sidecars of recently read books. Only books whose highlights
changed are sent, through the same outbox and retries. Their text is never logged.

Shelf sync (docs/device-queues.md#following-a-shelf): a quiet check of the device's queue on
start, wake, Wi-Fi back, and every Core.FETCH_INTERVAL, which downloads queued books by itself
when the device's Auto-download setting in Lectern is on, and deletes the copies it downloaded
of books that left the followed shelf when clean-up is on.

Device report (docs/koreader-plugin.md#device-dashboard): each check that reached Lectern (and
Sync now) also sends battery, free space, versions, and the books on the device. One that cannot
go waits beside the outbox for its next drain; it never schedules anything itself.

Whether the network is up is decided by trying the request (short timeouts), never by
KOReader's NetworkMgr alone: on jailbroken Kindles whose Wi-Fi is brought up outside KOReader
it reports offline forever. NetworkMgr only decides how soon to retry.
]]

local ConfirmBox = require("ui/widget/confirmbox")
local DataStorage = require("datastorage")
local Device = require("device")
local InfoMessage = require("ui/widget/infomessage")
local LuaSettings = require("luasettings")
local NetworkMgr = require("ui/network/manager")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local logger = require("logger")
local _ = require("gettext")

local Core = require("lectern_core")
local transport = require("lectern_http")

local ok_json, json = pcall(require, "rapidjson")
if not ok_json then json = require("json") end

-- kosync's SYNC_STRATEGY values
local SYNC_PROMPT, SYNC_SILENT, SYNC_DISABLE = 1, 2, 3

local Lectern = WidgetContainer:extend{
    name = "lectern",
    is_doc_only = false,
}

local function ago(ts)
    local d = os.time() - (ts or 0)
    if d < 60 then return _("just now") end
    if d < 3600 then return string.format(_("%d min ago"), math.floor(d / 60)) end
    if d < 86400 then return string.format(_("%d h ago"), math.floor(d / 3600)) end
    return string.format(_("%d days ago"), math.floor(d / 86400))
end

local function pluginInstance(name)
    local ok, PluginLoader = pcall(require, "pluginloader")
    if ok and PluginLoader.getPluginInstance then
        local ok2, inst = pcall(PluginLoader.getPluginInstance, PluginLoader, name)
        if ok2 then return inst end
    end
end

local function online()
    local ok, res = pcall(NetworkMgr.isOnline, NetworkMgr)
    return ok and res
end

function Lectern:init()
    self.store = LuaSettings:open(DataStorage:getSettingsDir() .. "/lectern.lua")
    self.core = Core.new{
        http = transport,
        json = json,
        store = self.store,
        plugin_dir = self.path,
        logger = logger,
        configure = function(config) self:configureKOReader(config) end,
    }
    self.retry_attempt = 0
    self.retry_task = function() self:retry() end
    self.annotations_task = function()
        if not self.retry_pending then self:flush() end
    end
    self.fetch_task = function() self:autoFetch() end
    -- Home Wi-Fi or Tailscale (docs/home-network.md): checked again after start, wake, and
    -- network changes, and every Core.ROUTE_TTL while KOReader runs.
    self.route_task = function()
        if not self.core:isPaired() or not self.core:lanUrl() then return end
        self.core:route(true)
        UIManager:scheduleIn(Core.ROUTE_TTL, self.route_task)
    end
    if not Lectern._started then
        -- First start of this KOReader process: the device may be on another network than when
        -- it last ran, so the home address is not trusted until it proves itself again.
        Lectern._started = true
        self:recheckHome(3)
    elseif self.core:lanUrl() then
        UIManager:scheduleIn(Core.ROUTE_TTL, self.route_task)
    end
    if self.ui and self.ui.menu then
        self.ui.menu:registerToMainMenu(self)
    end
    if self.ui and self.ui.document then
        self:hookKOSync()
    end
    UIManager:nextTick(function() self:start() end)
end

-- --- first start ---------------------------------------------------------------------------

function Lectern:start()
    if self.core:bootstrapSecret() and not self.core:isPaired() then
        self:trySetup()
    elseif self.core:pending() > 0 then
        self:retrySoon(2)
    end
    if self.core:isPaired() then self:fetchSoon(10) end
end

local function inTime(seconds)
    if seconds < 60 then return string.format(_("%d s"), seconds) end
    return string.format(_("%d min"), math.floor(seconds / 60))
end

--- Trade the setup secret now, whatever NetworkMgr says (on Kindles whose Wi-Fi is managed
-- outside KOReader it reports offline forever). A failed request is the "network down"
-- signal: retry with backoff. Quiet unless the reader asked (Sync now).
function Lectern:trySetup(interactive)
    local result, why = self.core:bootstrap()
    self.setup_pending = result == "retry"
    if not self.setup_pending then
        -- Settled (e.g. by Sync now): drop the earlier attempt's backoff, or automatic pushes
        -- would wait for that stale retry.
        self.retry_attempt = 0
        self.retry_pending = false
        UIManager:unschedule(self.retry_task)
    end
    if result == "ok" then
        UIManager:show(InfoMessage:new{
            text = _("Lectern is set up.\n\nProgress sync and the Lectern catalogue (Search → OPDS catalog) are configured for this device."),
            timeout = 6,
        })
    elseif result == "invalid" then
        UIManager:show(InfoMessage:new{
            text = _("This plugin's setup link was already used, expired, or revoked.\n\nIn Lectern, open Devices → this device → KOReader plugin → Show pairing code, then choose Lectern → Pair with a code here."),
        })
    elseif result == "retry" then
        -- Tailscale (e.g. KUAL) may still be coming up, or Wi-Fi is off: back off.
        self.retry_attempt = self.retry_attempt + 1
        local delay = self.core:retryDelay(self.retry_attempt)
        self:scheduleRetry(delay)
        if interactive then
            UIManager:show(InfoMessage:new{
                text = string.format(_("Lectern is not reachable yet (%s).\n\nCheck Wi-Fi and Tailscale. Setup tries again by itself in %s."), why or "offline", inTime(delay)),
            })
        end
    end
end

-- --- writing KOReader's own settings -------------------------------------------------------

--- Edit kosync's settings table in place wherever it lives: the loaded plugin (whose cached
-- copy would otherwise overwrite a file edit), settings/kosync.lua, or, on KOReader before
-- 2026.06, the "kosync" key of the global settings.
function Lectern:editKOSync(fn)
    local inst = (self.ui and self.ui.kosync) or pluginInstance("kosync")
    if inst and type(inst.settings) == "table" then
        fn(inst.settings)
        if inst.settings_obj then
            inst.settings_obj:saveSetting("settings", inst.settings)
            inst.settings_obj:flush()
        else
            G_reader_settings:saveSetting("kosync", inst.settings)
            G_reader_settings:flush()
        end
        return inst
    end
    local file = LuaSettings:open(DataStorage:getSettingsDir() .. "/kosync.lua")
    local t = file:readSetting("settings")
    if type(t) ~= "table" then
        t = { sync_backward = SYNC_DISABLE, checksum_method = 0, send_metadata = false }
    end
    fn(t)
    file:saveSetting("settings", t)
    file:flush()
    local old = G_reader_settings:readSetting("kosync")
    if type(old) == "table" then
        fn(old)
        G_reader_settings:saveSetting("kosync", old)
        G_reader_settings:flush()
    end
end

function Lectern:editOPDS(fn)
    local inst = (self.ui and self.ui.opds) or pluginInstance("opds")
    if inst and inst.loadSettings then pcall(inst.loadSettings, inst) end
    if inst and type(inst.servers) == "table" then
        fn(inst.servers)
        if inst.settings and inst.settings.saveSetting then
            inst.settings:saveSetting("servers", inst.servers)
            inst.settings:flush()
        else
            G_reader_settings:saveSetting("opds_servers", inst.servers)
            G_reader_settings:flush()
        end
        return
    end
    local file = LuaSettings:open(DataStorage:getSettingsDir() .. "/opds.lua")
    local servers = file:readSetting("servers")
    if type(servers) ~= "table" then
        servers = G_reader_settings:readSetting("opds_servers") or {}
    end
    fn(servers)
    file:saveSetting("servers", servers)
    file:flush()
    local old = G_reader_settings:readSetting("opds_servers")
    if type(old) == "table" then
        fn(old)
        G_reader_settings:saveSetting("opds_servers", old)
        G_reader_settings:flush()
    end
end

function Lectern.kosyncFields(t, config)
    local o = config.options
    t.custom_server = config.server_url .. "/"
    t.username = config.username
    t.userkey = config.userkey
    t.auto_sync = o.auto_sync and true or false
    t.pages_before_update = (o.pages_before_update or 0) > 0 and o.pages_before_update or nil
    t.sync_forward = o.sync_forward == "prompt" and SYNC_PROMPT or SYNC_SILENT
    if t.sync_backward == nil then t.sync_backward = SYNC_DISABLE end
end

function Lectern.opdsEntry(servers, config)
    local url = config.opds_url .. "/opds"
    local entry
    for _, s in ipairs(servers) do
        if s.lectern_device == config.device_id or s.url == url then entry = s break end
    end
    if not entry then
        entry = {}
        table.insert(servers, 1, entry)
    end
    entry.title = "Lectern"
    entry.url = url
    entry.username = config.username
    entry.password = config.app_password
    entry.lectern_device = config.device_id
end

--- No trusted address: KOReader's own sync and the Lectern catalogue entry lose their
-- credentials (a placeholder host is no barrier; any DNS can answer it). The plugin keeps
-- them and writes them back once an address is trusted again.
function Lectern:disarmKOReader(config)
    self:editKOSync(function(t)
        if t.username == config.username then t.userkey = nil t.custom_server = nil end
    end)
    self:editOPDS(function(servers)
        for _, s in ipairs(servers) do
            if s.lectern_device == config.device_id then s.password = nil s.username = nil end
        end
    end)
end

function Lectern:configureKOReader(config)
    if not config.userkey then return end
    if not config.server_url then return self:disarmKOReader(config) end
    local inst = self:editKOSync(function(t) Lectern.kosyncFields(t, config) end)
    if inst and inst.registerEvents then pcall(inst.registerEvents, inst) end
    if config.opds_url then
        self:editOPDS(function(servers) Lectern.opdsEntry(servers, config) end)
    else
        self:disarmKOReader({ username = "\0", device_id = config.device_id })
    end
    if config.options.offline_mode == "wifi" and Device:hasWifiToggle() then
        G_reader_settings:saveSetting("wifi_enable_action", "turn_on")
    end
end

-- --- automatic sync ------------------------------------------------------------------------

function Lectern:managed()
    return self.core:isPaired() and self.core:options().auto_sync
end

--- Wrap the reader's kosync instance. Automatic (non-interactive) pushes go through the
-- outbox: sent now when online, otherwise kept and retried quietly. Automatic pulls are
-- skipped while offline (kosync pulls by itself on NetworkConnected). Anything the reader asks
-- for explicitly (Push/Pull in kosync's menu) is left to kosync.
function Lectern:hookKOSync()
    local k = self.ui.kosync
    if not k or k._lectern_hooked then return end
    k._lectern_hooked = true
    local plugin = self
    local orig_update, orig_get = k.updateProgress, k.getProgress

    if self:managed() and type(k.settings) == "table" then
        -- kosync disables auto sync at start when Wi-Fi's "Action when Wi-Fi is off" is not
        -- "turn on"; this wrapper never turns Wi-Fi on behind the reader's back, so keep it on.
        local o = self.core:options()
        k.settings.auto_sync = true
        k.settings.pages_before_update = (o.pages_before_update or 0) > 0 and o.pages_before_update or nil
        k.settings.sync_forward = o.sync_forward == "prompt" and SYNC_PROMPT or SYNC_SILENT
    end

    k.updateProgress = function(ks, ensure_networking, interactive, on_suspend)
        if interactive or not plugin:managed() then
            return orig_update(ks, ensure_networking, interactive, on_suspend)
        end
        plugin:queueCurrent(ks)
        if not online() and ensure_networking and plugin.core:options().offline_mode == "wifi" then
            NetworkMgr:runWhenOnline(function() plugin:flush() end)
        elseif online() or not plugin.retry_pending then
            -- NetworkMgr may say offline while the network works (Wi-Fi managed outside
            -- KOReader): try anyway. Once a send failed, the backoff takes it from there.
            plugin:flush()
        end
    end

    k.getProgress = function(ks, ensure_networking, interactive)
        if interactive or not plugin:managed() or online() then
            return orig_get(ks, ensure_networking, interactive)
        end
        if ensure_networking and plugin.core:options().offline_mode == "wifi" then
            return orig_get(ks, ensure_networking, interactive)
        end
        -- Offline: no prompt; kosync's own NetworkConnected handler pulls later.
    end
end

function Lectern:deviceName(ks)
    return (ks and ks.settings and ks.settings.kosync_hostname) or Device.model
end

function Lectern:queueCurrent(ks, force_annotations)
    local ok, err = pcall(function()
        local digest = ks:getDocumentDigest()
        if not digest then return end
        self.core:queuePush{
            document = digest,
            progress = ks:getLastProgress(),
            percentage = ks:getLastPercent(),
            device = self:deviceName(ks),
            device_id = ks.device_id or G_reader_settings:readSetting("device_id"),
        }
    end)
    if not ok then self.core:log("warn", "could not read the position:", err) end
    self:queueAnnotations(ks, force_annotations)
end

--- The open book's highlights (KOReader 2024.07 and later keep them in ReaderAnnotation).
function Lectern:queueAnnotations(ks, force)
    local list = self.ui and self.ui.annotation and self.ui.annotation.annotations
    if type(list) ~= "table" then return false end
    local ok, queued = pcall(function()
        local digest = ks:getDocumentDigest()
        if not digest then return false end
        return self.core:queueAnnotations(digest, Core.annotationsFrom(list), self:deviceName(ks), force)
    end)
    if not ok then self.core:log("warn", "could not read the highlights:", queued) end
    return ok and queued
end

--- Highlights in the sidecars of recently read books (not the open one: queueCurrent has its
-- newer copy). Only books whose highlights changed since they were last sent are queued.
function Lectern:scanHistory()
    local ok_h, ReadHistory = pcall(require, "readhistory")
    local ok_d, DocSettings = pcall(require, "docsettings")
    if not ok_h or not ok_d or type(ReadHistory.hist) ~= "table" then return 0 end
    local open = self.ui and self.ui.document and self.ui.document.file
    local device, n = self:deviceName(self.ui and self.ui.kosync), 0
    for i, item in ipairs(ReadHistory.hist) do
        if i > Core.HISTORY_SCAN then break end
        local file = item.file
        if file and file ~= open and not item.dim then
            local ok, queued = pcall(function()
                if DocSettings.hasSidecarFile and not DocSettings:hasSidecarFile(file) then return false end
                local ds = DocSettings:open(file)
                local digest = ds:readSetting("partial_md5_checksum")
                local list = ds:readSetting("annotations")
                if not digest or type(list) ~= "table" then return false end
                return self.core:queueAnnotations(digest, Core.annotationsFrom(list), device)
            end)
            if ok and queued then n = n + 1 end
        end
    end
    return n
end

--- A highlight was made, edited, or deleted in the open book: send soon, quietly.
function Lectern:onAnnotationsModified()
    local k = self.ui and self.ui.kosync
    if not self:managed() or not k or not self.ui.document then return end
    if self:queueAnnotations(k) then
        UIManager:unschedule(self.annotations_task)
        UIManager:scheduleIn(15, self.annotations_task)
    end
end

--- Send the outbox now; on failure retry later with backoff while the network stays up.
function Lectern:flush()
    UIManager:unschedule(self.annotations_task)
    local sent, left, why, books = self.core:drain()
    if left == 0 then
        self.retry_attempt = 0
        self.retry_pending = false
        UIManager:unschedule(self.retry_task)
    elseif why ~= "unauthorized" then
        self.retry_attempt = self.retry_attempt + 1
        self:scheduleRetry(self.core:retryDelay(self.retry_attempt))
    end
    return sent, left, why, books
end

function Lectern:scheduleRetry(seconds)
    UIManager:unschedule(self.retry_task)
    UIManager:scheduleIn(seconds, self.retry_task)
    self.retry_pending = true
end

--- NetworkMgr is only a scheduling hint: when it says online, retry in ``seconds``;
-- otherwise keep (or start) the backoff, whose retries still send a request when due.
function Lectern:retrySoon(seconds)
    if online() then return self:scheduleRetry(seconds) end
    if not self.retry_pending then
        self.retry_attempt = math.max(self.retry_attempt, 1)
        self:scheduleRetry(self.core:retryDelay(self.retry_attempt))
    end
end

function Lectern:retry()
    self.retry_pending = false
    if self.setup_pending then
        self:trySetup()
        if self.setup_pending then return end -- trySetup scheduled the next try
        self.retry_attempt = 0
    end
    if self.core:pending() == 0 then return end
    self:flush()
end

--- Back to the Tailscale address now (KOReader's own sync and catalogue too), and check the
-- home address again in ``delay`` seconds.
function Lectern:recheckHome(delay)
    self.core:forgetHome()
    if self.core:lanUrl() then
        UIManager:unschedule(self.route_task)
        UIManager:scheduleIn(delay, self.route_task)
    end
end

function Lectern:onNetworkDisconnected()
    self.core:forgetHome()
end

function Lectern:onNetworkConnected()
    -- Tailscale may need a few seconds after Wi-Fi to bring the tailnet up.
    self.retry_attempt = 0
    self:recheckHome(3)
    if self.setup_pending or self.core:pending() > 0 then
        self:scheduleRetry(3)
    end
    self:fetchSoon(5)
end

function Lectern:onResume()
    -- Woken up, maybe elsewhere (Wi-Fi managed outside KOReader sends no network events).
    self:recheckHome(5)
    if self.core:pending() > 0 then self:retrySoon(5) end
    self:fetchSoon(8)
end

function Lectern:onCloseWidget()
    UIManager:unschedule(self.retry_task)
    UIManager:unschedule(self.route_task)
    UIManager:unschedule(self.annotations_task)
    UIManager:unschedule(self.fetch_task)
    self.retry_pending = false
end

-- --- shelf sync: the quiet check of the queue -----------------------------------------------

function Lectern:fetchSoon(seconds)
    if not self:managed() then return end
    UIManager:unschedule(self.fetch_task)
    UIManager:scheduleIn(seconds, self.fetch_task)
end

--- The file open in the reader right now, which clean-up never deletes.
function Lectern:isOpen(path)
    local doc = self.ui and self.ui.document
    return doc ~= nil and doc.file == path
end

--- Quietly check the queue: delete copies of books that left the followed shelf (clean-up)
-- and, with Auto-download on in Lectern, download up to Core.AUTO_BATCH queued books, one per
-- UI tick, stopping at the first failure (offline, or Lectern's hourly cap: 429); not while a
-- book is open, since downloads block the UI. Automatic
-- checks happen at most once per Core.FETCH_MIN, and only with Sync automatically on;
-- ``status`` (from Sync now) skips both and the request. Never prompts. Returns how many books
-- it is downloading and how many it deleted.
function Lectern:autoFetch(status)
    local forced = status ~= nil
    if not status then
        UIManager:unschedule(self.fetch_task)
        if not self:managed() then return 0, 0 end
        local now = os.time()
        local last = self.store:readSetting("last_fetch_at") or 0
        if now - last >= 0 and now - last < Core.FETCH_MIN then
            UIManager:scheduleIn(Core.FETCH_MIN - (now - last), self.fetch_task)
            return 0, 0
        end
        UIManager:scheduleIn(Core.FETCH_INTERVAL, self.fetch_task)
        self.store:saveSetting("last_fetch_at", now)
        self.store:flush()
        status = self.core:status()
        if not status then return 0, 0 end
    end
    local deleted = self.core:cleanUp(status.remove, function(path) return self:isOpen(path) end)
    if deleted > 0 and self.ui and self.ui.file_chooser then
        pcall(self.ui.file_chooser.refreshPath, self.ui.file_chooser)
    end
    -- Downloads block the UI, so a quiet check leaves them while a book is open; Sync now
    -- (``forced``) and the next check in the file browser get them.
    local reading = not forced and self.ui and self.ui.document ~= nil
    local items = status.auto_download == true and not reading
        and self.core:toDownload(status.queue and status.queue.items, Core.AUTO_BATCH) or {}
    local planned = #items
    local dir = self:downloadDir()
    local function nextBook()
        local item = table.remove(items, 1)
        if not item then return end
        local path, err = self.core:download(item, dir)
        if not path then
            self.core:log("info", "automatic download stopped:", err)
            return
        end
        if #items > 0 then UIManager:nextTick(nextBook) end
    end
    nextBook()
    -- The device report rides on this check, which just reached Lectern: sent now unless
    -- positions or highlights are waiting, whose pending retry then carries it.
    self:queueReport()
    if self.core:pending() == 0 then self.core:drainReport() end
    return planned, deleted
end

--- What the Devices dashboard shows (docs/koreader-plugin.md#device-dashboard): battery,
-- free space, versions, and the books KOReader knows. Read only during a check that already
-- reached Lectern; anything unreadable on this device is left out.
function Lectern:queueReport()
    local info = { books = self:knownBooks() }
    pcall(function()
        if Device.hasBattery and not Device:hasBattery() then return end
        local powerd = Device:getPowerDevice()
        info.battery = powerd:getCapacity()
        info.charging = powerd:isCharging() and true or false
    end)
    pcall(function()
        info.free_bytes = require("util").diskUsage(self:downloadDir()).available
    end)
    pcall(function()
        info.koreader_version = require("version"):getCurrentRevision()
    end)
    self.core:queueReport(info)
end

--- Recently read books with a sidecar (the first Core.HISTORY_SCAN of the history): path,
-- KOReader digest, and position; the open book's position as the reader has it now.
function Lectern:knownBooks()
    local ok_h, ReadHistory = pcall(require, "readhistory")
    local ok_d, DocSettings = pcall(require, "docsettings")
    if not ok_h or not ok_d or type(ReadHistory.hist) ~= "table" then return {} end
    local open = self.ui and self.ui.document and self.ui.document.file
    local out = {}
    for i, item in ipairs(ReadHistory.hist) do
        if i > Core.HISTORY_SCAN then break end
        local file = item.file
        if file and not item.dim then
            pcall(function()
                if DocSettings.hasSidecarFile and not DocSettings:hasSidecarFile(file) then return end
                local ds = DocSettings:open(file)
                local book = {
                    path = file,
                    digest = ds:readSetting("partial_md5_checksum"),
                    percentage = ds:readSetting("percent_finished"),
                }
                local k = self.ui and self.ui.kosync
                if file == open and k and k.getLastPercent then
                    local ok, p = pcall(k.getLastPercent, k)
                    if ok and p then book.percentage = p end
                end
                out[#out + 1] = book
            end)
        end
    end
    return out
end

-- --- Sync now ------------------------------------------------------------------------------

--- Always tries, whatever NetworkMgr says, and always says what happened: set up or synced,
-- refused, or not reachable with the actual error and when it tries again.
function Lectern:syncNow()
    if not self.core:isPaired() then
        if self.core:bootstrapSecret() then return self:trySetup(true) end
        return self:showPair()
    end
    self.core:route(true)
    local k = self.ui and self.ui.kosync
    if k and self.ui.document then self:queueCurrent(k, true) end
    self:scanHistory()
    local sent, left, why, books = self:flush()
    if k and self.ui.document and k.getProgress then
        pcall(k.getProgress, k, false, false)
    end
    local status, err = self.core:status()
    local got = status and self:autoFetch(status) or 0
    local text
    if why == "unauthorized" or err == "unauthorized" then
        text = _("Lectern refused this device's key. It was revoked or paired again elsewhere.\n\nUse Lectern → Pair with a code.")
    elseif left > 0 or not status then
        text = string.format(_("Lectern is not reachable right now (%s). %d item(s) (positions, highlights of a book) will be sent automatically when it is."), why or err or "offline", left)
        if left > 0 and self.retry_pending then
            text = text .. string.format(_("\n\nNext try in %s."), inTime(self.core:retryDelay(self.retry_attempt)))
        end
    else
        text = string.format(_("Synced with Lectern.\n%d position(s) sent. %d book(s) waiting for this device."), sent, status.queue and status.queue.pending or 0)
        if (books or 0) > 0 then
            text = text .. string.format(_("\nHighlights and notes of %d book(s) sent."), books)
        end
        if got > 0 then
            text = text .. string.format(_("\n%d book(s) downloading."), got)
        end
    end
    UIManager:show(InfoMessage:new{ text = text, timeout = 5 })
end

-- --- books sent to this device --------------------------------------------------------------

function Lectern:downloadDir()
    local dir = G_reader_settings:readSetting("download_dir") or G_reader_settings:readSetting("home_dir")
    if not dir and Device.home_dir then dir = Device.home_dir end
    return dir or DataStorage:getDataDir()
end

function Lectern:showQueue()
    local status, err = self.core:status()
    if not status then
        UIManager:show(InfoMessage:new{ text = string.format(_("Could not reach Lectern (%s)."), err or "offline") })
        return
    end
    local items = status.queue and status.queue.items or {}
    if #items == 0 then
        UIManager:show(InfoMessage:new{ text = _("Nothing is waiting for this device.\n\nUse Send to device in Lectern's library."), timeout = 4 })
        return
    end
    local Menu = require("ui/widget/menu")
    local menu
    local item_table = {}
    for _, it in ipairs(items) do
        item_table[#item_table + 1] = {
            text = it.title,
            mandatory = (type(it.author) == "string" and it.author) or "",
            callback = function()
                UIManager:close(menu)
                self:download(it)
            end,
        }
    end
    menu = Menu:new{
        title = string.format(_("Sent to %s"), self.store:readSetting("label") or _("this device")),
        item_table = item_table,
        is_borderless = true,
        is_popout = false,
        covers_fullscreen = true,
    }
    UIManager:show(menu)
end

function Lectern:download(item)
    local info = InfoMessage:new{ text = string.format(_("Downloading %s…"), item.title) }
    UIManager:show(info)
    UIManager:forceRePaint()
    local path, err = self.core:download(item, self:downloadDir())
    UIManager:close(info)
    if not path then
        UIManager:show(InfoMessage:new{ text = string.format(_("Download failed (%s)."), err) })
        return
    end
    UIManager:show(ConfirmBox:new{
        text = string.format(_("Saved to\n%s\n\nOpen it now?"), path),
        ok_text = _("Open"),
        ok_callback = function()
            local ReaderUI = require("apps/reader/readerui")
            ReaderUI:showReader(path)
        end,
    })
end

-- --- connection status and pairing ----------------------------------------------------------

function Lectern:showStatus()
    local lines = {}
    local function add(k, v) lines[#lines + 1] = k .. ": " .. tostring(v) end
    add(_("Server"), self.core:activeUrl() or _("unknown"))
    local lan = self.core:lanUrl()
    if lan then
        add(_("Home Wi-Fi address"), lan .. (self.core:activeUrl() == lan and _(" (in use)") or ""))
        add(_("Tailscale address"), self.core:serverUrl() or _("unknown"))
    end
    add(_("Account"), self.core:username() or _("unknown"))
    add(_("Device"), self.store:readSetting("label") or "—")
    local function show()
        add(_("Status"), self.core:statusLine(online(), ago))
        add(_("Waiting to send"), #self.core:outbox())
        add(_("Highlights waiting (books)"), #self.core:annotationOutbox())
        local last = self.store:readSetting("last_push_at")
        add(_("Last push"), last and ago(last) or _("never"))
        UIManager:show(InfoMessage:new{ text = table.concat(lines, "\n") })
    end
    if not self.core:isPaired() then return show() end
    local status = self.core:status()
    if status then
        add(_("Books waiting"), status.queue and status.queue.pending or 0)
    end
    show()
end

function Lectern:showPair()
    local MultiInputDialog = require("ui/widget/multiinputdialog")
    local dialog
    dialog = MultiInputDialog:new{
        title = _("Pair with Lectern"),
        fields = {
            { description = _("Server"), text = self.core:serverUrl() or "http://100.", hint = "http://100.x.y.z:8080" },
            { description = _("Username"), text = self.core:username() or "", hint = _("Lectern username") },
            { description = _("6-digit code from Lectern → Devices"), text = "", hint = "123456", input_type = "number" },
        },
        buttons = { {
            { text = _("Cancel"), id = "close", callback = function() UIManager:close(dialog) end },
            { text = _("Pair"), is_enter_default = true, callback = function()
                local f = dialog:getFields()
                UIManager:close(dialog)
                self:doPair(f[1], f[2], f[3])
            end },
        } },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function Lectern:doPair(server, username, code)
    server = (server or ""):gsub("%s", ""):gsub("/+$", "")
    username = (username or ""):gsub("^%s+", ""):gsub("%s+$", "")
    code = (code or ""):gsub("%D", "")
    if not Core.validServerUrl(server) or username == "" or #code ~= 6 then
        UIManager:show(InfoMessage:new{ text = _("Enter the server address (http://100.x.y.z:8080, or Lectern's home Wi-Fi address), your username and the 6-digit code.") })
        return
    end
    local result = self.core:pair(server, username, code)
    local text = ({
        ok = _("Paired. Progress sync and the Lectern catalogue are set up."),
        invalid = _("That code did not work. Codes last 5 minutes and allow 5 tries; show a new one in Lectern if needed."),
        rate_limited = _("Too many tries. Wait a few minutes, then try again."),
        no_fallback = _("Not paired: Lectern did not say its Tailscale address, and the home Wi-Fi address cannot stand in for it. Show a new code in Lectern and pair with the Tailscale address (http://100.x.y.z:8080) as Server."),
    })[result] or _("Lectern is not reachable. Check Wi-Fi and Tailscale, then try again.")
    UIManager:show(InfoMessage:new{ text = text, timeout = result == "ok" and 4 or nil })
end

-- --- menu ----------------------------------------------------------------------------------

function Lectern:addToMainMenu(menu_items)
    local core = self.core
    menu_items.lectern = {
        text = _("Lectern"),
        sorting_hint = "tools",
        sub_item_table = {
            {
                text_func = function() return core:statusLine(online(), ago) end,
                keep_menu_open = true,
                callback = function() self:showStatus() end,
                separator = true,
            },
            { text = _("Sync now"), callback = function() self:syncNow() end },
            {
                text = _("Books sent to this device"),
                enabled_func = function() return core:isPaired() end,
                callback = function() self:showQueue() end,
                separator = true,
            },
            {
                text = _("Sync automatically"),
                help_text = _("Pull the latest position when a book opens; push it when the book closes, when the device sleeps, and every few pages. Highlights and notes go to Lectern too. Without Wi-Fi, they wait and are sent when the connection is back."),
                checked_func = function() return core:options().auto_sync end,
                callback = function() core:setOption("auto_sync", not core:options().auto_sync) end,
            },
            {
                text_func = function()
                    local n = core:options().pages_before_update or 0
                    return n > 0 and string.format(_("Also push every %d pages"), n) or _("Also push every N pages: off")
                end,
                enabled_func = function() return core:options().auto_sync end,
                keep_menu_open = true,
                callback = function(touchmenu)
                    local SpinWidget = require("ui/widget/spinwidget")
                    UIManager:show(SpinWidget:new{
                        title_text = _("Push every N pages"),
                        info_text = _("0 turns it off. Pushes need the network already on; they never turn Wi-Fi on."),
                        value = core:options().pages_before_update or Core.DEFAULT_PAGES,
                        value_min = 0, value_max = 200, value_step = 1, value_hold_step = 10,
                        default_value = Core.DEFAULT_PAGES,
                        callback = function(spin)
                            core:setOption("pages_before_update", spin.value)
                            if touchmenu then touchmenu:updateItems() end
                        end,
                    })
                end,
            },
            {
                text = _("When another device is further ahead"),
                sub_item_table = {
                    { text = _("Jump there"), radio = true,
                      checked_func = function() return core:options().sync_forward ~= "prompt" end,
                      callback = function() core:setOption("sync_forward", "silent") end },
                    { text = _("Ask first"), radio = true,
                      checked_func = function() return core:options().sync_forward == "prompt" end,
                      callback = function() core:setOption("sync_forward", "prompt") end },
                },
            },
            {
                text = _("When Wi-Fi is off"),
                sub_item_table = {
                    { text = _("Wait and send later (saves battery)"), radio = true,
                      checked_func = function() return core:options().offline_mode ~= "wifi" end,
                      callback = function() core:setOption("offline_mode", "queue") end },
                    { text = _("Turn Wi-Fi on to sync"), radio = true,
                      checked_func = function() return core:options().offline_mode == "wifi" end,
                      callback = function() core:setOption("offline_mode", "wifi") end },
                },
                separator = true,
            },
            { text = _("Pair with a code…"), callback = function() self:showPair() end },
        },
    }
end

return Lectern
