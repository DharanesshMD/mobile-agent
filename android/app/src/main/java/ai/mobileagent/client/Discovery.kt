package ai.mobileagent.client

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.InetAddress
import java.net.URL
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Finds the owner's computer on the local network, so only the pairing code has to be typed.
 *
 * Step 1: a UDP question (`MOBILE_AGENT_DISCOVER v1`) to the Wi-Fi's directed broadcast address and to 255.255.255.255, port 8767. While the web app's "Allow phones on this
 * Wi-Fi" door is open the computer answers with {"app","v","port","name"}; the address is the reply's source address.
 * Step 2, only if nothing answered (many routers drop broadcast): GET /api/hello on every host of the phone's own /24 on the gateway port, short timeouts, many threads.
 * Nothing secret is part of this: no pairing code, no token. The owner still types the code.
 */
object Discovery {
    const val UDP_PORT = 8767
    const val HTTP_PORT = 8765
    private val MAGIC = "MOBILE_AGENT_DISCOVER v1".toByteArray()
    private const val LISTEN_MS = 1500L
    private const val SCAN_TIMEOUT_MS = 300
    private const val SCAN_THREADS = 48

    data class Found(val ip: String, val port: Int, val name: String) {
        val url get() = "http://$ip:$port"
    }

    /** `found` is empty when nothing answered; `noWifi` says there is no local network to look on at all. */
    data class Result(val found: List<Found>, val noWifi: Boolean = false, val how: String = "")

    /** One running search. cancel() stops it and closes its sockets; it never calls back after that. */
    class Search(private val ctx: Context, private val done: (Result) -> Unit) {
        private val cancelled = AtomicBoolean(false)
        @Volatile private var socket: DatagramSocket? = null
        @Volatile private var pool: java.util.concurrent.ExecutorService? = null

        fun start() {
            Thread({ run() }, "discovery").start()
        }

        fun cancel() {
            cancelled.set(true)
            runCatching { socket?.close() }
            pool?.shutdownNow()
        }

        private fun finish(r: Result) {
            if (!cancelled.get()) done(r)
        }

        private fun run() {
            val lan = localNetwork(ctx)
            if (lan == null) { finish(Result(emptyList(), noWifi = true)); return }
            val (net, own, prefix) = lan
            val viaUdp = try { broadcast(net, own, prefix) } catch (_: Exception) { emptyList() }
            if (cancelled.get()) return
            if (viaUdp.isNotEmpty()) { finish(Result(viaUdp, how = "broadcast")); return }
            val viaScan = try { scan(net, own) } catch (_: Exception) { emptyList() }
            finish(Result(viaScan, how = if (viaScan.isEmpty()) "" else "scan"))
        }

        private fun broadcast(net: Network, own: Inet4Address, prefix: Int): List<Found> {
            val sock = DatagramSocket().also { socket = it }
            val found = LinkedHashMap<String, Found>()
            try {
                runCatching { net.bindSocket(sock) }  // answer over the Wi-Fi even when mobile data is the default route
                sock.broadcast = true
                val targets = listOf(directed(own, prefix), InetAddress.getByName("255.255.255.255"))
                val end = System.currentTimeMillis() + LISTEN_MS
                var nextSend = 0L
                val buf = ByteArray(1024)
                while (!cancelled.get() && System.currentTimeMillis() < end) {
                    if (System.currentTimeMillis() >= nextSend) {  // asked twice: a lost packet should not decide the result
                        for (t in targets) runCatching { sock.send(DatagramPacket(MAGIC, MAGIC.size, t, UDP_PORT)) }
                        nextSend = System.currentTimeMillis() + 600
                    }
                    sock.soTimeout = 200
                    val p = DatagramPacket(buf, buf.size)
                    try { sock.receive(p) } catch (_: java.net.SocketTimeoutException) { continue }
                    parse(String(p.data, 0, p.length), p.address.hostAddress ?: continue)?.let { found["${it.ip}:${it.port}"] = it }
                }
            } finally {
                runCatching { sock.close() }
            }
            return found.values.toList()
        }

        private fun scan(net: Network, own: Inet4Address): List<Found> {
            val b = own.address
            val executor = Executors.newFixedThreadPool(SCAN_THREADS).also { pool = it }
            val jobs = (1..254).filter { it != (b[3].toInt() and 0xff) }.map { host ->
                val ip = "${b[0].toInt() and 0xff}.${b[1].toInt() and 0xff}.${b[2].toInt() and 0xff}.$host"
                Callable<Found?> { if (cancelled.get()) null else hello(net, ip) }
            }
            val out = mutableListOf<Found>()
            try {
                for (f in executor.invokeAll(jobs, 20, TimeUnit.SECONDS)) {
                    runCatching { f.get() }.getOrNull()?.let { out += it }
                }
            } finally {
                executor.shutdownNow()
            }
            return out
        }

        /** GET http://ip:8765/api/hello; only a JSON answer that says app == "mobile-agent" counts. */
        private fun hello(net: Network, ip: String): Found? {
            var c: HttpURLConnection? = null
            return try {
                c = net.openConnection(URL("http://$ip:$HTTP_PORT/api/hello")) as HttpURLConnection
                c.connectTimeout = SCAN_TIMEOUT_MS
                c.readTimeout = SCAN_TIMEOUT_MS
                if (c.responseCode != 200) null else parse(c.inputStream.bufferedReader().use { it.readText().take(2000) }, ip)
            } catch (_: Exception) {
                null
            } finally {
                c?.disconnect()
            }
        }
    }

    fun parse(text: String, ip: String): Found? = try {
        val j = JSONObject(text)
        val port = j.optInt("port", HTTP_PORT)
        if (j.optString("app") == "mobile-agent" && port in 1..65535) Found(ip, port, j.optString("name").ifBlank { ip }.take(60)) else null
    } catch (_: Exception) {
        null
    }

    /** The Wi-Fi (or wired) network with an IPv4 address: the network, our address on it and the prefix length. Null when the phone is on no local network. */
    private fun localNetwork(ctx: Context): Triple<Network, Inet4Address, Int>? {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        for (n in cm.allNetworks) {
            val caps = cm.getNetworkCapabilities(n) ?: continue
            if (!(caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) || caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET))) continue
            val la = cm.getLinkProperties(n)?.linkAddresses?.firstOrNull { it.address is Inet4Address && !it.address.isLoopbackAddress } ?: continue
            return Triple(n, la.address as Inet4Address, la.prefixLength)
        }
        return null
    }

    /** The directed broadcast address of the network the address is on (every host bit set). */
    fun directed(own: Inet4Address, prefix: Int): InetAddress {
        val a = own.address
        val mask = if (prefix <= 0) 0 else (-1 shl (32 - prefix.coerceAtMost(32)))
        val ip = ((a[0].toInt() and 0xff) shl 24) or ((a[1].toInt() and 0xff) shl 16) or ((a[2].toInt() and 0xff) shl 8) or (a[3].toInt() and 0xff)
        val b = ip or mask.inv()
        return InetAddress.getByAddress(byteArrayOf((b ushr 24).toByte(), (b ushr 16).toByte(), (b ushr 8).toByte(), b.toByte()))
    }
}
