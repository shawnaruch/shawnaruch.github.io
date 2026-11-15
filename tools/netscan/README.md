# netscan

Finds every device on the home network and lists, per device, the local
(no vendor cloud) way to read it and what you need to supply.

Run on a machine inside the house. Python 3.8+, no installs, no root.

```
python3 netscan.py --oui
```

Output goes to `./netscan-output/` (gitignored): `inventory.md` to read,
`inventory.json` for the dashboards.

**This repo is public. The output maps your network. Don't commit it.**

Tips for a fuller scan:
- Run it from a wired machine on the main LAN, not a guest Wi-Fi.
- Devices on separate VLANs or a guest network won't show unless you pass
  their subnet with `--cidr` and the scanner can route to it.
- Battery devices (Zigbee, Z-Wave, Thread, BLE sensors) are not on IP at all.
  They show up only as their hub or bridge.
