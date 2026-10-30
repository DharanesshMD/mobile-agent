package ai.mobileagent.client

/** Connection state shared between the service and the screen (read by the screen once a second). */
object Status {
    @Volatile var connection: String = "not running"
    @Volatile var lastError: String = ""
    @Volatile var lastOp: String = ""
}
