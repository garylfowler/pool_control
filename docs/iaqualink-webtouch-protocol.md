# iAqualink WebTouch protocol notes (captured 2026-09-14)

Device: "Fowler Pool", serial <serial redacted>, device_type iaqua, AquaLink RS (systemType 0, "RS")
Owner portal: https://www.iaqualink.net (Angular). Login API: https://prod.zodiac-io.com/users/v1/login
Portal API: https://prm.iaqualink.net/v2  (device list gives touchLink per device)
idToken lives in cookie `idToken` on iaqualink.net (JWT, ~1 hr, refresh via refreshToken)

## WebTouch session
1. GET https://prm.iaqualink.net/v2/webtouch/init?actionID=<touchLink>   (header Authorization: <idToken>, withCredentials)
   -> JSON (real keys, verified 2026-09-14 from the add-on): label, systemType, serverConnection (stream URL),
      actionIdMasterId, actionIdMasterStart, actionIdMasterSTB, actionIdMasteReset (sic). Values are bare action ids
      (e.g. "NL_XYxCH3nqtqVa"); the page prepends "?actionID=" to build masterID/masterStart/masterSTB/masterReset.
   Observed: masterID=?actionID=NL_XYxCH3nqtqVa  masterStart=NL_C6Z0RtNmGrln  masterSTB=NL_93bfK7weFaiq  masterReset=NL_UUOwXZ91KduQ
   serverConnection=https://webtouch.iaqualink.net/5E/2NFZ6TQMRFY9H1LUDE64/NX3DQ58V5E
2. Stream: GET serverConnection (long-lived, withCredentials/cookies). Body is a series of
   <script type='text/javascript'>parent.printNL(code, "params")</script> chunks.
   code 23 -> params = page id (currentID). code 24 -> "index||state||image||label||value" (a button)
   code 28 -> date/time. Page 1 = Home, 15 = Menu, 54 = Devices (Other Devices On/Off), 30 = VSP Adjust/Set Speed
3. Command: POST https://prm.iaqualink.net/v2/webtouch/command
   headers: Content-Type: application/json, Authorization: <idToken>
   body: {"actionID":"NL_XYxCH3nqtqVa","command":"<n>","dt":"<ms>"}  (built from "?actionID=..&command=n&dt=ms")
   Response body is empty; new screen arrives on the stream.

## Command numbers
Nav (any page): 1 Home, 2 Menu, 3 OneTouch, 4 Help, 5 Back, 6 Status
Page buttons: command = 17 + button index (from code-24 "index")
Home (page 1): 0 Filter Pump, 1 Spa, 2 Pool Heat, 3 Spa Heat, 4 Waterfall, 5 Jet Pump, 6 Air Blower, 7 Other Devices (=24)
Devices (page 54): 0 Filter Pump, 1 Spa, 2 VSP1 Spd ADJ (=19), 3 Pool Heat, 4 Spa Heat, 5 Heat Pump, 6 Waterfall,
  7 Jet Pump, 8 Air Blower, 9 Aux4, 10 Aux5, 11 Aux6, 12 Aux7, 13 Spa Mode, 14 Clean Mode
VSP page (30): presets (index -> command):
  0 Pool 2950 (=17)  1 Spa 2700 (=18)  2 FAST CLEAN 3450 (=19)  3 HIGH SUN 3200 (=20)
  4 Pool Heat 3000 (=21)  5 Spa Heat 2000 (=22)  6 Cloudy 2800 (=23)  7 At Night 900 (=24)
  state field ==1 marks the currently selected preset (Pool 2950 was selected).
  Custom RPM: keypad Enter -> masterSTB + "&command=128&text=<rpm>"

Sequence to set a preset speed: Home(1) -> Other Devices(24) -> ADJ(19) -> preset(17..24)

## Live test 2026-09-14 (confirmed working)
- On page 30, command=23 (Cloudy) -> stream: "24: 0||0||0||Pool||2950", "24: 6||1||0||Cloudy||2800", "25: 0||2800"
- command=17 (Pool) -> stream: "24: 6||0||0||Cloudy||2800", "24: 0||1||0||Pool||2950", "25: 0||2950"
- code 25 -> "0||<rpm>" = current pump RPM readout. Round trip ~1 s.

## Login and device discovery (verified 2026-09-14)
1. POST https://prod.zodiac-io.com/users/v1/login
   JSON body: {"api_key": "EOOEMOW4YR6QNB07", "email": "<email>", "password": "<password>"}
   Response includes userPoolOAuth: {IdToken, RefreshToken, ExpiresIn (3600)} (+ id, session_id, authentication_token).
   Refresh: POST https://prod.zodiac-io.com/users/v1/refresh  body {"email": "<email>", "refresh_token": "<RefreshToken>"} -> same userPoolOAuth shape.
   (Constants and shapes cross-checked with the open-source iaqualink-py library.)
2. GET https://prm.iaqualink.net/v2/userId      header Authorization: Bearer <IdToken>
   -> {"session_user_id": "<session id redacted>", "session_id": ..., "userLevel": "user", ...}
3. GET https://prm.iaqualink.net/v2/users/<session_user_id>/locations   header Authorization: Bearer <IdToken>
   -> {"locations": [{"Id": "<session id redacted>", "Name": "Fowler Pool", "device_type": "iaqua",
        "serial_number": "<serial redacted>", "touchLink": "<touchLink redacted>", "editLink": ..., "statusLink": ...}], "messages": []}
   touchLink is the actionID for webtouch init.
Header note: the portal API (prm.iaqualink.net/v2/*) uses "Authorization: Bearer <IdToken>". The webtouch
init/command calls send the raw token: "Authorization: <IdToken>" (no Bearer prefix) - as the WebTouch page does.

## Home page info values (from DOM ids, code 25 on page 1)
1_25_0 = pool temp ("82º"), 1_25_1 = air temp ("63º"), 1_25_3 = spa temp slot (empty while spa off),
1_25_4 label "Pool Temp", 1_25_5 label "Air Temp", 1_25_2 label slot for spa. Verify the code-25 index->slot mapping on first run.

## Learned during go-live (2026-09-14, from the running add-on)
- The stream body starts with a tiny HTML shell, then each message arrives as its own chunk padded with spaces
  to 4096 chars and prefixed with a timestamp like "9/14/2026 11:56:28 PM". printNL codes are QUOTED strings:
  parent.printNL('24','0||1||8||Filter||Pump'). Codes can be words: 'OFFLINE' (device not reachable for this
  session) and '2D' (page label text, e.g. 'AquaLink Touch').
- The panel stays silent until the session's start command is sent: POST command with actionID = actionIdMasterStart
  and command=1 (the page does this ~2.5 s after loading).
- After a session dies (add-on restart), new sessions get printNL('OFFLINE','') and the stream closes, for
  roughly 60-90 s, until the old session times out server-side. The client pauses 30 s between attempts.
- A page's buttons arrive over several chunks after the page id; wait for the buttons you need, not just the id.
- Request the stream with `Accept-Encoding: identity`. With gzip the padded 4 KB messages compress to almost
  nothing and the compressor holds many of them back, so pages arrive in bursts minutes apart and navigation
  times out. Uncompressed, a page change arrives in ~1 s (verified 2026-09-15, add-on 0.2.1).
- SAFETY: screen buttons are addressed by position (command = 17 + index) and the same position means
  different things on different pages: index 7 is "Other Devices" on Home but "Jet Pump" on the Devices page.
  Never send a page button unless a page message for the expected page arrived *after* your previous command
  (the add-on tracks a page sequence number). If no fresh page arrives, drop the stream and reconnect rather
  than retry blind. A blind "Home, 24" retry loop turned the user's jet pump on (found 2026-09-17).
