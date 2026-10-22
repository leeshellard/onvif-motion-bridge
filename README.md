# ONVIF Motion Bridge

Get **non-UniFi cameras into UniFi Protect, with motion events**.

Protect can adopt third-party cameras over ONVIF, but many cameras (for example Eufy, or anything
that only offers RTSP) either aren't ONVIF devices or don't send ONVIF motion events, so Protect
records them but never knows when something moves. This bridge fixes both:

1. **[go2rtc](https://github.com/AlexxIT/go2rtc)** pulls each camera's RTSP stream and re-encodes it into a
   clean H.264 stream that Protect is happy with (optional, on an Intel GPU).
2. **[onvif-server](https://github.com/daniela-hase/onvif-server)** by Daniela Hasenbring presents each stream
   as a virtual ONVIF camera, each with its own IP/MAC. **This repo adds a patch** so the virtual cameras
   also support the ONVIF **Events (PullPoint)** service, plus a small HTTP API to raise and clear motion.
3. **`motion.py`**, a lightweight video motion detector, watches a tiny greyscale copy of each stream and
   calls that API when motion starts and stops. Protect then sees ONVIF motion events.

```
camera --RTSP--> go2rtc --> onvif-server (+ motion patch) --ONVIF--> UniFi Protect
                   \--> motion.py --POST /motion/<name>--/
```

You can also trigger motion from anything else, e.g. a Home Assistant automation calling the API.

## The motion detector

- Compares each frame with a slowly adapting background, after compensating for overall brightness
  changes (clouds, auto-exposure).
- Ignores whole-picture changes (lights switching on, IR night-mode switching).
- Needs **solid blocks** of change rather than scattered speckle, which filters out flickering light and noise.
- Per-camera tuning and **mask areas** (trees, timestamp overlays) via environment variables.
- Saves the last 40 trigger frames with their change masks (PGM) to help tuning.
- Uses about 1-2% CPU per camera at 160x90, 4 fps, decoding on the GPU via VAAPI.

## Requirements

- A Linux host or container (tested on a Debian 13 Proxmox LXC) with Node.js, Python 3 and ffmpeg.
- An extra network interface per virtual camera (e.g. extra `netN` devices on a Proxmox LXC), so each
  virtual camera gets its own MAC and IP. Give them DHCP reservations.
- Optional: an Intel GPU passed through (`/dev/dri/renderD128`) for re-encoding and decoding.
  Without one, remove the VAAPI options and use a stream the camera already serves cleanly.

## Install

```bash
apt install -y git nodejs npm python3 ffmpeg
useradd --system onvif && useradd --system go2rtc
usermod -aG render onvif && usermod -aG render go2rtc      # GPU access, if used

git clone https://github.com/leeshellard/onvif-motion-bridge.git /opt/onvif-motion-bridge

# onvif-server with the motion patch
git clone https://github.com/daniela-hase/onvif-server.git /opt/onvif-server
cd /opt/onvif-server && git apply /opt/onvif-motion-bridge/patches/onvif-server-motion-events.patch && npm install

# go2rtc: download the linux binary for your CPU from https://github.com/AlexxIT/go2rtc/releases
mkdir -p /opt/go2rtc   # put the go2rtc binary here, chmod +x

# motion detector
mkdir -p /opt/onvif-motion && cp /opt/onvif-motion-bridge/motion/motion.py /opt/onvif-motion/

# config
cp /opt/onvif-motion-bridge/config/config.example.yaml /opt/onvif-server/config.yaml
cp /opt/onvif-motion-bridge/config/go2rtc.example.yaml /opt/go2rtc/go2rtc.yaml
cp /opt/onvif-motion-bridge/config/onvif-motion.example /etc/default/onvif-motion
openssl rand -hex 24 > /opt/onvif-server/motion.token && chown onvif /opt/onvif-server/motion.token && chmod 600 /opt/onvif-server/motion.token
chown go2rtc /opt/go2rtc/go2rtc.yaml && chmod 600 /opt/go2rtc/go2rtc.yaml      # holds camera passwords
# now edit the three config files: camera RTSP addresses, MACs, names

# routing, so each virtual camera answers from its own interface
cp /opt/onvif-motion-bridge/scripts/virtual-camera-routes /usr/local/sbin/

cp /opt/onvif-motion-bridge/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now virtual-camera-routes go2rtc onvif-server onvif-motion@cam1 onvif-motion@cam2
```

Then in UniFi Protect, **adopt third-party cameras** and enter the camera login when asked.

## Motion API

```bash
curl -X POST -H "Authorization: Bearer $(cat /opt/onvif-server/motion.token)" \
     "http://127.0.0.1:8099/motion/Camera1?state=on"     # state=on | off
```

Motion clears itself after `maxMotionSeconds` if no `off` arrives. Set `motionApi.host` to the
container's LAN IP if another machine (e.g. Home Assistant) should call it, and keep the token secret.

## Tuning motion

Watch the log (`journalctl -fu onvif-motion@cam1`): every 5 minutes it reports the peak change and how
many scattered-change frames it ignored. Trigger frames are saved in `/var/lib/onvif-motion/<stream>/`
(left half: picture, right half: what changed).

| Setting | Effect |
|---|---|
| `PIXEL_DIFF` | Lower = notices fainter changes |
| `ON_PERCENT` | Lower = smaller moving objects count (wide views need less) |
| `ON_FRAMES` | Higher = ignores very brief flickers |
| `HOLD_SECONDS` | How long motion stays on after movement stops |
| `MASK` | Areas to ignore, e.g. `0.85,0,1,0.6` = right 15%, top 60% |

Add `_<stream>` to any setting to override it for one camera, e.g. `ON_PERCENT_cam2=0.4`.

## Credits and licence

- [onvif-server](https://github.com/daniela-hase/onvif-server) © 2024 Daniela Hasenbring, MIT licence. The patch in
  `patches/` modifies it. See `NOTICE`.
- [go2rtc](https://github.com/AlexxIT/go2rtc) by Alexey Khit, MIT licence.
- Everything else here: MIT licence (see `LICENSE`). Not affiliated with Ubiquiti or Eufy.
