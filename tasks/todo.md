# Todo

## Planning
- [x] Create private repo `DharanesshMD/mobile-agent`
- [x] Research Android OS options, write docs/plan.md
- [x] Confirm track: Indie Developer
- [ ] Pick Android host and verify binder support
- [x] Local spike: Android 14 emulator on Mac, ADB screenshot + UI tree + tap (spike/probe.py)
- [ ] Cloud spike later: Redroid or Cuttlefish on a Linux host

## Next
- [x] Agent loop with Meta model (observe, plan, act, verify): agent/loop.py, tested live on emulator
- [ ] Harden loop: memory of seen info, better stuck recovery, richer actions (long press, text field focus)
- [ ] Web chatbot with live device view
- [x] FastAPI service + web chatbot: runs, SSE, approvals, handoff, OTP card, takeover (agent/service.py, web/index.html)
- [ ] Pairing + phone-facing channel (WebSocket/push) so the Android client can receive requests and send OTPs
- [ ] Android client app: pairing, SMS/OTP relay with per-request approval, push-to-talk
- [ ] Play Store system image (google_apis_playstore) and Google sign-in handoff with a throwaway account
