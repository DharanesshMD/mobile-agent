package ai.mobileagent.client

import android.content.Context

/** What the app remembers: which server it is paired with and the device token the server issued (sealed with a Keystore key). */
object Prefs {
    private fun p(c: Context) = c.applicationContext.getSharedPreferences("agent", Context.MODE_PRIVATE)

    fun server(c: Context): String = p(c).getString("server", "") ?: ""
    fun enabled(c: Context): Boolean = p(c).getBoolean("enabled", false)

    fun token(c: Context): String {
        val prefs = p(c)
        val sealed = prefs.getString("token_sealed", null)
        if (sealed != null) return SecureStore.open(sealed) ?: ""
        val legacy = prefs.getString("token", null)  // an older build stored it in the clear: seal it now
        if (!legacy.isNullOrEmpty()) {
            prefs.edit().putString("token_sealed", SecureStore.seal(legacy)).remove("token").apply()
            return legacy
        }
        return ""
    }

    fun savePairing(c: Context, server: String, token: String) {
        p(c).edit().putString("server", server.trim().trimEnd('/')).putString("token_sealed", SecureStore.seal(token)).remove("token").apply()
    }

    fun setEnabled(c: Context, on: Boolean) {
        p(c).edit().putBoolean("enabled", on).apply()
    }

    /** Disconnect: forget the token and stop; the server address stays so pairing again is quick. */
    fun forget(c: Context) {
        p(c).edit().remove("token").remove("token_sealed").putBoolean("enabled", false).apply()
        OpsLog.clear(c)
    }
}
