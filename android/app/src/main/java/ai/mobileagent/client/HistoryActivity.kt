package ai.mobileagent.client

import android.app.Activity
import android.graphics.Typeface
import android.os.Bundle
import android.view.MotionEvent
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** What the agent has done: the phone bot's recent tasks from the server, and this phone's own log of every operation (time, operation, app). Pull down to refresh. */
class HistoryActivity : Activity() {
    private lateinit var tasks: LinearLayout
    private lateinit var ops: LinearLayout
    private lateinit var note: TextView
    private lateinit var scroll: ScrollView
    private val day = SimpleDateFormat("d MMM HH:mm", Locale.getDefault())
    private val sec = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
    private var downY = 0f

    private fun text(s: String, size: Float = 14f, bold: Boolean = false, pad: Int = 4) = TextView(this).apply {
        this.text = s
        textSize = size
        if (bold) setTypeface(typeface, Typeface.BOLD)
        setPadding(0, pad, 0, pad)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = "Mobile Agent history"
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(40, 48, 40, 40) }
        col.addView(text("History", 24f, true))
        note = text("Pull down to refresh.", 13f)
        col.addView(note)
        col.addView(Button(this).apply { text = "Refresh"; isAllCaps = false; setOnClickListener { load() } })
        col.addView(text("Tasks the assistant ran on this phone", 17f, true, 12))
        tasks = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        col.addView(tasks)
        col.addView(text("Everything it did on this phone (kept here only: no screenshots, no typed text)", 17f, true, 12))
        ops = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        col.addView(ops)
        scroll = ScrollView(this).apply { addView(col) }
        // Pull down at the top to refresh (a plain ScrollView has no built-in gesture and the app avoids extra libraries).
        scroll.setOnTouchListener { v, ev ->
            when (ev.action) {
                MotionEvent.ACTION_DOWN -> downY = ev.rawY
                MotionEvent.ACTION_UP -> if (scroll.scrollY == 0 && ev.rawY - downY > 220) load()
            }
            false
        }
        setContentView(scroll)
    }

    override fun onResume() {
        super.onResume()
        load()
    }

    private fun load() {
        renderOps()
        val server = Prefs.server(this)
        val token = Prefs.token(this)
        if (token.isEmpty() || server.isEmpty()) {
            note.text = "Not paired: no tasks to show."
            tasks.removeAllViews()
            return
        }
        note.text = "Loading…"
        Thread {
            try {
                val rows = Pairing.history(server, token)
                runOnUiThread { renderTasks(rows); note.text = "Updated ${sec.format(Date())}. Pull down to refresh." }
            } catch (e: Exception) {
                runOnUiThread { note.text = "Could not load tasks: ${e.message}" }
            }
        }.start()
    }

    private fun renderTasks(rows: org.json.JSONArray) {
        tasks.removeAllViews()
        if (rows.length() == 0) tasks.addView(text("No tasks yet. Ask the assistant to do something on this phone."))
        for (i in 0 until rows.length()) {
            val r = rows.getJSONObject(i)
            val at = if (r.isNull("at")) "" else day.format(Date((r.getDouble("at") * 1000).toLong()))
            val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(0, 10, 0, 10) }
            box.addView(text("${r.optString("status")} · $at", 12f))
            box.addView(text(r.optString("goal"), 15f, true, 2))
            val result = r.optString("result")
            if (result.isNotBlank()) box.addView(text(result, 13f, false, 2))
            val apps = r.optJSONArray("apps")
            if (apps != null && apps.length() > 0) box.addView(text("Apps: " + (0 until apps.length()).joinToString(", ") { apps.getString(it) }, 12f, false, 2))
            tasks.addView(box)
        }
    }

    private fun renderOps() {
        ops.removeAllViews()
        val list = OpsLog.all(this)
        if (list.isEmpty()) ops.addView(text("Nothing yet."))
        for (e: JSONObject in list.take(200)) {
            ops.addView(text("${sec.format(Date(e.getLong("t")))}  ${e.optString("op")}  ${e.optString("pkg")}", 12f, false, 1).apply { typeface = Typeface.MONOSPACE })
        }
    }
}
