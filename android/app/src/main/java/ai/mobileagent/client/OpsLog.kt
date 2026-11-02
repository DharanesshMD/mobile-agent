package ai.mobileagent.client

import android.content.Context
import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject

/**
 * A local record of what the agent did on this phone: time, operation, app. Never screenshots, never typed text.
 * A ring buffer of the last 200 entries, kept in memory and saved a moment after each change.
 */
object OpsLog {
    private const val MAX = 200
    private val entries = ArrayList<JSONObject>()
    private var loaded = false
    private val ui = Handler(Looper.getMainLooper())
    private var pendingSave: Runnable? = null

    private fun prefs(c: Context) = c.applicationContext.getSharedPreferences("oplog", Context.MODE_PRIVATE)

    @Synchronized private fun load(c: Context) {
        if (loaded) return
        loaded = true
        runCatching {
            val arr = JSONArray(prefs(c).getString("entries", "[]"))
            for (i in 0 until arr.length()) entries.add(arr.getJSONObject(i))
        }
    }

    @Synchronized fun add(c: Context, op: String, pkg: String) {
        load(c)
        entries.add(JSONObject().put("t", System.currentTimeMillis()).put("op", op).put("pkg", pkg))
        while (entries.size > MAX) entries.removeAt(0)
        pendingSave?.let { ui.removeCallbacks(it) }
        val app = c.applicationContext
        pendingSave = Runnable { save(app) }.also { ui.postDelayed(it, 3000) }
    }

    @Synchronized private fun save(c: Context) {
        prefs(c).edit().putString("entries", JSONArray(entries).toString()).apply()
    }

    /** Newest first. */
    @Synchronized fun all(c: Context): List<JSONObject> {
        load(c)
        return entries.reversed()
    }

    @Synchronized fun clear(c: Context) {
        load(c)
        entries.clear()
        save(c)
    }
}
