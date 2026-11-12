# X-Fi DSP driver for Linux (test package)

Runs Creative's X-Fi effects **on the card's own DSP** under Linux: Crystalizer, SVM, EQ (Game mode), the Entertainment
processing chain, or 7.1 surround through CMSS-3D Headphone, with the taskbar volume and the microphone working like in
Windows.

- **What is (and isn't) in this package:** no Creative driver files and no Creative DSP program, except 7 to 9 single
  program words (depending on the mode) that could not be matched to the files. The DSP code and data are stored only as *references*
  (file, section, offset) into Creative's files, which each user provides from their own Windows driver; the
  firmware is built from them locally (`firmware/*.bin` then contains Creative's code - keep it to yourself).
  What the recipes (`recipes/*/winmode_seq.txt`, `mode.json`) do contain is functional hardware configuration,
  recorded for interoperability: the register values Creative's Windows driver writes to operate the card (mixer
  routing, converter setup, parameter values, effect settings), plus fill patterns and pointer/descriptor values.
  That is a method of operating the hardware, not creative expression - see e.g. 17 U.S.C. § 102(b) (US) and
  Art. 6 of the EU Software Directive (interoperability). This is not legal advice.
- Tested with one card (Sound Blaster X-Fi XtremeGamer **SB073x**, PCI `1102:0005`, subsystem `1102:0031`) in two systems:
  - Nobara/Fedora 44, Linux **7.2.6** and **7.3.1**, PipeWire 1.6 + WirePlumber 0.5, card in a StarTech PCIe-to-PCI adapter (PEX1PCI1);
  - Xubuntu (Ubuntu 26.04.1 LTS) Different PC, Linux **7.0.0**, card in a native PCI slot. 
  The scripts accept any 20K1 card (PCI `1102:0005`), but the DSP sequence was recorded on SB073x: other 20K1 models are untested.
  20K2 cards (Titanium) are not supported. Kernels older than 7.0 are untested and may not compile.
- `game` and `ent` are built from Windows set to **2/2.1 speakers** (stereo + subwoofer). `hp71` takes **7.1 input** and
  turns it into stereo for **headphones** with CMSS-3D Headphone; it was tested on the Nobara system only (Linux 7.3.1).
  Surround output to real speakers (4.1/5.1/7.1 on the rear jacks) is not available yet, and neither is a subwoofer output: the Center/Subwoofer jack stayed silent in `game` `ent` and `hp71`. In `hp71` the LFE channel plays through the headphones.

Three modes (each is a firmware; you pick one, switching later is one command). `hp71` is the default and has the most
features; `game` and `ent` are less developed. `hp71` It was made for headphones, and speakers are untested so far,
but it probably works with them just fine:

| | hp71 (Game, 7.1 + Headphones) | game (Game, 2/2.1) | ent (Entertainment, 2/2.1) |
|---|---|---|---|
| Crystalizer + level, SVM | yes | yes | yes |
| Taskbar volume, mic monitor, microphone recording | yes | yes | yes |
| EQ | 10 presets, your own 10-band EQ, Bass, Treble, level | 2 presets + level | 10 presets, your own 10-band EQ, Bass, Treble, level |
| CMSS-3D | **yes**, headphone surround | - | - (no effect with 2/2.1 speakers, on Windows too) |
| EAX, Mic Environment | yes, heard through Mic Monitor; mic recordings stay dry | - | - |
| Recording source What U Hear | yes | - | yes |
| Bass Boost | yes | - | on/off, at Windows' default level |
| Input from apps | 7.1 (stereo apps stay left/right) | stereo | stereo |
| Card's internal rate | 2x (96 kHz) | 2x (96 kHz) | 4x (192 kHz) |

---

## 1. Put the Creative files in place

You need two files from Creative's Windows driver, version 2.30.0012. You don't need Windows.

**Easiest:** download Creative's X-Fi driver installer, `SBXF_PCDRV_L11_2_30_0012.exe`, from [Creative's support site](https://support.creative.com/downloads/download.aspx?nDownloadId=100271), and drop it into the **`creative/`** folder of this package. That's all: `./build.sh` in step 3 unpacks the two files from it and checks them. It doesn't run the installer. This needs `7z` (package `7zip` or `p7zip`).

**Or copy them by hand** into the **`creative/`** folder of this package:

| File | Size | sha256 |
|---|---|---|
| `ha20x2k.sys` | 1573888 | `bb0a78ffc0b34bb40c848e82fb71cf5849537d1ab1fad6a49621465976000be9` |
| `CTEXFIFX.sys` | 1429000 | `4fa5e49e03d45ff70970f26f0f6cffd8ff4e30e04cb973d68bab78460f8cdafe` |

On a Windows PC with the Creative X-Fi driver installed they are in `C:\Windows\System32\drivers\`.
Only these exact versions work - `build.sh` checks them. Use the 64-bit files; the sizes tell them apart.

## 2. Install the build tools (once)

You need `python3`, `make`, `gcc`, the **kernel headers for your running kernel**, and the tools the scripts use:
`alsa-utils` (aplay, amixer, alsamixer), `pciutils` (lspci - rearm.sh finds the card with it), `psmisc` (fuser) and
`pactl` (part of PipeWire's Pulse support), and PyQt6 for the tray app:

```bash
sudo dnf install python3 python3-pyqt6 make gcc kernel-devel-$(uname -r) alsa-utils pciutils psmisc pulseaudio-utils   # Fedora / Nobara
```
```bash
sudo apt install python3 python3-pyqt6 make gcc linux-headers-$(uname -r) alsa-utils pciutils psmisc pulseaudio-utils # Debian / Ubuntu / Mint
```
```bash
sudo pacman -S python python-pyqt6 make gcc linux-headers alsa-utils pciutils psmisc libpulse                          # Arch
```

**Secure Boot (untested so far, please report):** if it is on, the kernel refuses unsigned modules ("Key was rejected
by service"). The scripts sign the driver with DKMS's signing key (they create it if needed, `mokutil` and `openssl`
must be installed), but the kernel only trusts that key once it's enrolled. Do that once, before testing:
```bash
sudo ./src/xfi_secureboot.sh enroll
```
It asks you to choose a one-time password. Reboot: a blue "MOK Manager" screen appears (press a key, it waits only
10 seconds), choose Enroll MOK, Continue, Yes, type the password, Reboot. `./src/xfi_secureboot.sh status` shows
whether it worked. If you install the driver (step 5), the installer does this for you.

**Nobara's Easy Anti-Cheat kernel (`kernel-eac`) cannot load this driver:** it only accepts modules signed by Nobara, whatever keys
you enroll. Use it with the distro's normal X-Fi driver (no DSP effects), and a normal kernel for this driver. Don't install
`kernel-eac-devel`: DKMS would then replace the distro's X-Fi driver for that kernel with one it refuses to load, and the card
would have no driver there.

## 3. Build

In this folder, as your normal user:

```bash
./build.sh
```

It checks the Creative files, builds the driver (`src/snd-ctxfi.ko`) and the `hp71` firmware
(`firmware/xfi20k1-dsp-hp71.bin`). For the stereo modes, name them: `./build.sh game`, `./build.sh ent`, or
`./build.sh all` for all three.

Then pick one of the three ways to run it:

| | Option | Lasts | After a kernel update |
|---|---|---|---|
| A | Test (step 4) | until the next reboot, then your normal driver is back | nothing to do |
| B | Install without DKMS (step 5) | survives reboots | `./build.sh` and install again; until then your distro's driver is used |
| C | Install with DKMS (step 5) | survives reboots | rebuilt automatically |

## 4. Option A: try it without installing anything permanently

Loads our driver until the next reboot; afterwards your normal driver is back.

**What this does to your running system** (read this first): it stops PipeWire/WirePlumber, **kills every process that
still holds a sound device open** (`fuser -k /dev/snd/*` - close music players, browsers with audio, voice chat first),
unloads the X-Fi driver currently in use, resets the card, loads our driver and starts PipeWire again. Other sound
cards are not touched, but programs using them may be closed too.

This loads the default mode, `hp71`:

```bash
./src/xfi_test.sh
```

For another mode, or to switch later, name it: `./src/xfi_test.sh game` or `./src/xfi_test.sh ent`. Build its firmware
first with `./build.sh game` or `./build.sh ent`.

It asks for your password and takes about 10 seconds. The output "X-Fi DSP" and the input "X-Fi DSP Mic" are
then the defaults, and the taskbar volume works. Effects: `alsamixer -c XFi` (F6 to pick the card), or e.g.
```bash
amixer -c XFi cset name='DSP Crystalizer Playback Switch' on
```

To clean up afterwards (removes the test firmware and the small PipeWire profile), then reboot:
```bash
./src/xfi_test.sh off
```
If you skip this, a reboot still brings your normal driver back; the leftover profile does no harm. **With a permanent install, do
run it after testing another mode:** the test changes the PipeWire channel layout, and this puts yours back.

After a kernel update `xfi_test.sh` rebuilds its own copy of the driver first. Installed drivers aren't affected by that.

## 5. Install permanently

Both options install the driver, the firmware, the PipeWire profile and an autostart service (`xfi-dsp.service`) that
loads the DSP in the background after boot. These commands install the default mode, `hp71`. For another mode put
`env XFI_MODE=game` (or `ent`) after `sudo`: `sudo env XFI_MODE=game ./src/xfi_dsp_install.sh install-dkms`. The install
builds that mode's firmware itself if `./build.sh` didn't.

**Option B, without DKMS:** the driver is installed for the running kernel only.
```bash
sudo ./src/xfi_dsp_install.sh install-nodkms
```

**Option C, with DKMS:** the driver is rebuilt automatically for every new kernel. Install DKMS first
(`sudo dnf install dkms` / `sudo apt install dkms` / `sudo pacman -S dkms`).
```bash
sudo ./src/xfi_dsp_install.sh install-dkms
```

With **Secure Boot** on (untested so far), the installer signs the driver and, unless its key is already enrolled, asks
for a one-time password: at the next boot confirm the key in the blue "MOK Manager" screen (Enroll MOK, Continue, Yes,
the password, Reboot).

Then **reboot**. A few seconds after logging in the DSP is running; check with:
```bash
cat /sys/kernel/xfi_dsp/winmode
```
(should say `running`). If the output/mic aren't selected automatically: `./src/xfi_profile.sh`.

- **With DKMS on Fedora/Nobara the install can take several minutes** at "Executing post-transaction command": the
  distribution rebuilds the initramfs of all kernels. Let it finish.
- DKMS moves the distro's `snd-ctxfi` aside for that kernel (it shows "Original modules exist" in `dkms status`);
  `uninstall` puts it back.
- **Switch mode or option later:** run the install again with the other `XFI_MODE` or command, reboot.
- **After a kernel update:** with DKMS nothing to do (check with `dkms status`). Without DKMS: `./build.sh` again,
  then `install-nodkms` again (with the same `XFI_MODE`) - until then your distro's normal X-Fi driver is used (switch PipeWire back with
  `./src/xfi_profile.sh analog`). If DKMS fails to build for a new kernel, the distro driver is used the same way.
- **Uninstall everything:**
  ```bash
  sudo ./src/xfi_dsp_install.sh uninstall
  ```
  then reboot.

### If the system freezes after installing

The autostart has a **crash guard**: if a DSP start never finished (the PC froze or was reset while it loaded), the
next boot does **not** start it again - just reboot once. The log then says so (`journalctl -b -u xfi-dsp`).
To try again later: `sudo rm /var/lib/xfi-dsp/starting`, then reboot.

The guard only reacts to a start that never finished, as after a freeze, a reset or power loss. Rebooting or shutting down
normally while the DSP is still starting doesn't trigger it, even from the login screen: the DSP starts at boot and
loads for a few seconds, sometimes while the login screen is already showing. If the DSP doesn't start
(the tray icon stays grey), `journalctl -b -u xfi-dsp.service` says why.
To turn the autostart off without uninstalling: `sudo touch /etc/xfi-dsp-noautostart`.

If it still freezes during boot (e.g. the driver itself, not the DSP start), keep the X-Fi driver from loading for
one boot: in the **GRUB menu** press `e` on your normal entry, add **`modprobe.blacklist=snd_ctxfi`** at the end of
the line starting with `linux`, boot with **Ctrl+X** (applies to that boot only), then uninstall:
```bash
sudo ./src/xfi_dsp_install.sh uninstall
```

## Tray app

The permanent install starts a tray icon at login: red bars while the DSP runs, grey when it doesn't. Click it for a panel with tabs for the effects,
the EQ and the microphone, and a Master Volume knob. It shows what the loaded mode has, and **restores your settings after every boot**, which the driver
alone doesn't do. Master and Mic Level stay PipeWire's, which remembers them. Close the panel with its ✕ or Esc, move it by its title strip. It follows your
desktop theme's colours; untick **Use system colors** in the tray icon's right-click menu for a dark blue look. To try it during a
test: `python3 src/xfi_tray.py`.

**EQ tab** (`hp71`, `ent`): a preset or your own curve (**Custom**: move any of the 10 band sliders, 31 Hz to 16 kHz, ±12 dB),
plus **Bass**, **Treble** (±12 dB) and the **EQ Level** (0 to −24 dB), all combinable as on Windows. Behind the band sliders
the tray draws the curve the card really applies; Bass, Treble and the level pull it away from the sliders. Very sharp shapes
can't be made by the EQ's wide bands, and the curve shows how close it gets. Big boosts distort on loud music (the tray
warns above +12 dB): lower the EQ Level then. A plain preset with Bass and Treble at 0 is Creative's own recorded EQ; anything
else is computed in the same format (within 0.3 dB of Creative's presets) and sent to the hidden control `DSP EQ Custom Bank`.


## PipeWire profiles

The card shows two profiles: **X-Fi DSP**, the one to use, and **Analog Stereo (normal driver)**, the fallback when our driver or the DSP isn't running, for example on
another kernel. The distro's analog surround and digital profiles are hidden, because they stay silent while the DSP runs. To get them back:
`./src/xfi_dsp_install.sh pipewire-off`.

## Controls (alsamixer -c XFi)

| Control | What it does |
|---|---|
| DSP Master | the volume (moved by the taskbar slider through PipeWire) |
| DSP Crystalizer / DSP Crystalizer Level | Crystalizer on/off and intensity |
| DSP SVM | Smart Volume Management |
| DSP EQ Preset / DSP EQ Level | EQ (game: presets A/B are two of Creative's presets; hp71 and ent: all 10 by name). Each preset has its own level: picking a preset resets it and the slider stays where it was. Your own 10-band EQ and Bass/Treble sliders on top of any preset are in the tray (see Tray app) |
| DSP CMSS-3D | hp71: headphone surround |
| DSP EAX | hp71: EAX level, bottom = off. No effect on music; with Mic Environment it changes your voice in Mic Monitor, not in mic recordings |
| DSP Mic Environment | hp71: sends the mic through EAX. Hear it with DSP Mic Monitor and DSP EAX on |
| DSP Bass Boost / DSP Bass Boost Level / DSP Bass Boost Cutoff | more bass. ent: on/off only, at Windows' default level. hp71: the level runs from 3 dB (0) to 12 dB (18) in 0.5 dB steps, 6 = Windows' default, and can clip at the top. The cutoff is in Hz, 10 to 200, 100 = Windows' default. What U Hear recordings don't include it |
| DSP Recording Source | hp71 and ent: `Mic`, or `What U Hear` = everything you hear (music, games, the mic with its effects). The mic alone stays dry. Also selectable as the input's port in your desktop's sound settings |
| DSP Mic Monitor | hear the microphone through the speakers |
| DSP Auto Mute Speakers | SB073x: silences the rear jacks while headphones are in the front panel header. Off by default |
| DSP Mic Capture Volume (Capture page, F4) | microphone recording level (top = Windows' 100) |
| DSP Mic Boost (Capture page, F4) | gain on the card's input chip, 0 to +20 dB in 1 dB steps, before everything else. Off at the start, the tray restores yours. Raises background noise by the same amount |

A graphical ALSA mixer such as QasMixer shows the same controls as switches and sliders.
In `alsamixer` DSP Master jumps from 0 to 15: its range ends at -50 dB, which is 15 on that scale, and 0 is mute. The taskbar and the tray panel go lower, in software.

## Good to know

- **Seat the card firmly and screw it down:** a card that moves in its slot, e.g. when plugging into its jacks, can crash
  (sound stops; the driver then stops touching it and `cat /sys/kernel/xfi_dsp/winmode` says to reboot) or freeze the whole PC.
- **Microphone silent?** Unplug and replug the jack, then reload or reboot. On one test the mic only came back after that.
- **People can barely hear you?** Raise DSP Mic Boost (tray: Mic Boost). Normal speech should peak around -10 dBFS.
- **Recording too loud or distorted?** Lower DSP Mic Capture Volume (shown as `DSP Mic`, Capture page, F4); its top is about +6 dB.
  It also sets the What U Hear level, which starts at 1.0 and is not changed by the taskbar volume.
- **What U Hear in voice chat:** the other people hear everything you hear, possibly their own voices too.
- **X-Fi mic gone from the taskbar mixer?** PipeWire drops it if something opens the device directly (e.g. `arecord -D hw:...`)
  at the wrong moment. `./src/xfi_profile.sh` brings it back.
- **hp71:** PipeWire shows a 7.1 output, also when you only have headphones - that's correct, CMSS-3D makes stereo from it.
  With CMSS-3D off you hear plain left/right.
- **Sleep/suspend** works: the DSP reloads by itself a few seconds after waking up.
- While the DSP runs, the analog/digital outputs of the normal Linux driver (the "Analog Stereo/Surround" profiles)
  are silent - use the "X-Fi DSP" profile.

## If something goes wrong

- `sudo ./src/rearm.sh` says "no Creative X-Fi 20K1 found": the card is not a 20K1 (`lspci -nn | grep -i creative`).
- The build fails with compiler errors: your kernel is probably too old for this code (tested: 7.0 to 7.3; it uses
  recent kernel interfaces).
- Winmode says `failed`, or no sound: send the output of
  ```bash
  sudo dmesg | grep -iE "ctxfi|windows mode|windows path|xfi"
  ```
  plus `lspci -nnv -d 1102:` and which firmware (game/ent/hp71) you used.
- To stop the DSP right away: `sudo ./src/dsp_halt.sh`; to get your normal driver back: reboot.
