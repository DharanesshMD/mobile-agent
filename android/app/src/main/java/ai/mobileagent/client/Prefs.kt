package ai.mobileagent.client

import android.content.Context

/** What the app remembers: which server it is paired with and the device token the server issued. */
object Prefs {
    private fun p(c: Context) = c.applicationContext.getSharedPreferences("agent", Context.MODE_PRIVATE)

    fun server(c: Context): String = p(c).getString("server", "http://10.0.2.2:8001") ?: "http://10.0.2.2:8001"
    fun token(c: Context): String = p(c).getString("token", "") ?: ""
    fun enabled(c: Context): Boolean = p(c).getBoolean("enabled", false)

    fun savePairing(c: Context, server: String, token: String) {
        p(c).edit().putString("server", server.trim().trimEnd('/')).putString("token", token).apply()
    }

    fun setEnabled(c: Context, on: Boolean) {
        p(c).edit().putBoolean("enabled", on).apply()
    }

    fun forget(c: Context) {
        p(c).edit().remove("token").putBoolean("enabled", false).apply()
    }
}
