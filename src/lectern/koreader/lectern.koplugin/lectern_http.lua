--[[ Blocking HTTP for the Lectern plugin: LuaSocket, short timeouts, no redirects.

transport{ method, url, headers, body, sink, timeout = {block, total} }
  -> status code, response body ("" when a sink was given)
  -> nil, nil, error text when the connection itself failed (Wi-Fi off, Tailscale down,
     timeout). The error text never contains the request, so it is safe to log.

KOReader's HTTP proxy setting (socket.http.PROXY) applies, which is what makes plain-http
OPDS work through tailscaled's proxy in userspace mode (docs/koreader-plugin.md).
]]

local http = require("socket.http")
local ltn12 = require("ltn12")
local ok_su, socketutil = pcall(require, "socketutil")
if not ok_su then socketutil = nil end

local function transport(req)
    local block = (req.timeout and req.timeout[1]) or 5
    local total = (req.timeout and req.timeout[2]) or 15
    if socketutil then
        socketutil:set_timeout(block, total)
    else
        http.TIMEOUT = block
    end
    local chunks = {}
    local headers = {}
    for k, v in pairs(req.headers or {}) do headers[k] = v end
    if req.body then
        headers["content-length"] = tostring(#req.body)
    end
    local ok, r1, r2 = pcall(http.request, {
        url = req.url,
        method = req.method or "GET",
        headers = headers,
        source = req.body and ltn12.source.string(req.body) or nil,
        sink = req.sink or ltn12.sink.table(chunks),
        redirect = false,
    })
    if socketutil then socketutil:reset_timeout() end
    -- Table form: (1, code, headers, status line) on success, (nil, error) on failure.
    if not ok then
        return nil, nil, "request failed"
    end
    if r1 == nil or type(r2) ~= "number" then
        return nil, nil, tostring(r2 or "no response")
    end
    return r2, table.concat(chunks)
end

return transport
