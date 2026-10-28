local _ = require("gettext")
return {
    name = "lectern",
    fullname = _("Lectern"),
    -- Sent in the device report; the Devices dashboard says "update available" when the
    -- server hands out a newer one. Raise it with every change to the plugin.
    version = "1.0",
    description = _([[Connects KOReader to your Lectern server: progress sync and the Lectern catalogue set up automatically, sync without pressing Push, and the books sent to this device.]]),
}
