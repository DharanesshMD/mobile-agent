package ai.mobileagent.client

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Keeps the connection to the server: one outbound WebSocket (works behind any router or firewall), reconnecting with backoff.
 * Each request runs on a worker thread and is answered with {"id","ok","result"|"error"}.
 */
class AgentService : Service() {
    companion object {
        private const val CHANNEL = "agent"
        private const val NOTIFICATION_ID = 7
        private const val ACTION_STOP = "ai.mobileagent.client.STOP"

        fun start(c: Context) {
            Prefs.setEnabled(c, true)
            c.startForegroundService(Intent(c, AgentService::class.java))
        }

        fun stop(c: Context) {
            Prefs.setEnabled(c, false)
            c.stopService(Intent(c, AgentService::class.java))
        }
    }

    private val workers = Executors.newCachedThreadPool()
    private val pinger = Executors.newSingleThreadScheduledExecutor()
    private val http = OkHttpClient.Builder().readTimeout(0, TimeUnit.MILLISECONDS).connectTimeout(10, TimeUnit.SECONDS).build()
    @Volatile private var running = false
    @Volatile private var socket: WebSocket? = null
    private var loop: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            Prefs.setEnabled(this, false)
            stopSelf()
            return START_NOT_STICKY
        }
        notify("Mobile Agent is starting…")
        if (!running) {
            running = true
            loop = Thread(::connectLoop, "agent-link").also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        socket?.close(1000, "stopped")
        pinger.shutdownNow()
        workers.shutdownNow()
        Status.connection = "not running"
        super.onDestroy()
    }

    private fun notify(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Mobile Agent", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, 1, Intent(this, AgentService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentTitle("Mobile Agent")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
        if (Build.VERSION.SDK_INT >= 34) startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else startForeground(NOTIFICATION_ID, n)
    }

    private fun wsUrl(): String {
        val base = Prefs.server(this)
        val ws = if (base.startsWith("https://")) "wss://" + base.removePrefix("https://") else "ws://" + base.removePrefix("http://")
        return "$ws/api/phone/ws?token=" + Uri_encode(Prefs.token(this))
    }

    private fun Uri_encode(s: String) = android.net.Uri.encode(s)

    private fun connectLoop() {
        var backoff = 1L
        while (running) {
            val closed = java.util.concurrent.CountDownLatch(1)
            var opened = false
            try {
                if (Prefs.token(this).isEmpty()) throw IllegalStateException("not paired")
                Status.connection = "connecting…"
                val ws = http.newWebSocket(Request.Builder().url(wsUrl()).build(), object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        opened = true
                        socket = webSocket
                        val size = Ops.size(this@AgentService)
                        webSocket.send(JSONObject().put("type", "hello").put("name", Build.MODEL).put("model", Build.MODEL)
                            .put("sdk", Build.VERSION.SDK_INT).put("size", org.json.JSONArray(size.toList())).toString())
                        Status.connection = "connected"
                        Status.lastError = ""
                        notify("Mobile Agent is connected")
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        workers.execute { answer(webSocket, text) }
                    }

                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) { closed.countDown() }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        Status.lastError = response?.let { "server answered ${it.code}" } ?: (t.message ?: t.javaClass.simpleName)
                        closed.countDown()
                    }
                })
                val ping = pinger.scheduleWithFixedDelay({ if (opened) ws.send("{\"type\":\"ping\"}") }, 25, 25, TimeUnit.SECONDS)
                while (running && !closed.await(1, TimeUnit.SECONDS)) { /* wait for the socket to end */ }
                ping.cancel(false)
                ws.cancel()
            } catch (e: Exception) {
                Status.lastError = e.message ?: e.javaClass.simpleName
            }
            socket = null
            if (!running) break
            if (opened) backoff = 1
            Status.connection = "reconnecting in ${backoff}s"
            notify("Mobile Agent is reconnecting…")
            try { Thread.sleep(backoff * 1000) } catch (_: InterruptedException) { break }
            backoff = (backoff * 2).coerceAtMost(30)
        }
    }

    private fun answer(ws: WebSocket, text: String) {
        val req = runCatching { JSONObject(text) }.getOrNull() ?: return
        if (!req.has("id")) return
        val reply = JSONObject().put("id", req.getInt("id"))
        val op = req.optString("op")
        Status.lastOp = op
        try {
            val result = Ops.handle(this, op, req.optJSONObject("args") ?: JSONObject())
            reply.put("ok", true).put("result", result ?: JSONObject.NULL)
        } catch (e: Throwable) {
            reply.put("ok", false).put("error", e.message ?: e.javaClass.simpleName)
        }
        ws.send(reply.toString())
    }
}
