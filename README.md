# X-Fi DSP for Linux

**Run the Sound Blaster X-Fi's own DSP effects on Linux: CMSS-3D headphone surround, Crystalizer, EQ, SVM, Bass Boost and more.**

The normal Linux driver plays sound through the X-Fi but leaves its DSP unused. This project starts the DSP the way Creative's Windows driver does, so the effects run on the card itself.

Your games and movies send up to 7.1 audio, the card turns it into surround for your headphones, and the taskbar volume and microphone work as usual.

> **Status: test release.** Developed and tested on one card, a Sound Blaster X-Fi XtremeGamer (SB073x). Reports from other cards are very welcome.

## Feature status for Headphone Mode

| Feature | Status |
|---|---|
| **Effects** | |
| CMSS-3D headphone surround for games, movies and music, up to 7.1 | ✅ |
| Crystalizer, with level slider | ✅ |
| SVM (Smart Volume Management) | ✅ |
| EQ: all 10 presets, with level slider | ✅ |
| Configurable 10-band EQ, with a live curve of what the card does | ✅ (tray) |
| Bass and Treble sliders, on top of any preset or your own EQ, as on Windows | ✅ (tray) |
| Bass Boost, with level slider (3 to 12 dB) | ✅ |
| Bass Boost cutoff slider | ❌ Not added yet, stays at 100 Hz |
| EAX on music or games | ➖ No effect, only audible through Mic Monitor, just like on Windows* |
| **Output** | |
| Stereo output on the green jack | ✅ |
| Front panel header (your PC case's headphone and mic jacks) | ✅ |
| Muting speakers when front panel headphones are plugged in (optional, off by default) | ✅ |
| Taskbar volume, controlling the DSP | ✅ |
| **Recording** | |
| Microphone recording and Mic Monitor | ✅ |
| Mic Boost, up to +20 dB | ✅ |
| EAX reverb on your voice through Mic Monitor (mic recordings stay dry) | ✅ |
| What U Hear (record everything you hear), selectable in your desktop's sound settings | ✅ |
| Line-in or S/PDIF | ❌ Not done |
| **System** | |
| Sleep and resume | ✅ |
| Automatic rebuild after kernel updates (DKMS) | ✅ |
| Tray app with all controls, restoring your settings after a reboot | ✅ |

This table is for the default mode, `hp71` (Game Mode with headphones and 7.1 downmix). Two stereo modes, `game` (Game Mode with speakers) and `ent` (Entertainment Mode with speakers), are also included. They have fewer features and aren't my priority right now. `ent` has Crystalizer, SVM, the EQ with Bass and Treble, Bass Boost, mic, Mic Monitor and What U Hear, but no CMSS-3D: with stereo speakers it has no effect, on Windows too.

Rear, side, center and subwoofer outputs (4.1 to 7.1) don't apply to headphone mode. They aren't supported yet, and would need to be added to `game` and `ent` modes.

## Compatibility

| | |
|---|---|
| **Tested card** | Sound Blaster X-Fi XtremeGamer (SB073x), PCI `1102:0005`, subsystem `1102:0031` |
| **Other 20K1 cards** | Untested. They may work, and the mic will probably stay silent |
| **20K2 cards (Titanium)** | Not supported |
| **Tested systems** | Nobara 44 (Linux 7.2 and 7.3), card in a StarTech PCIe-to-PCI adapter (PEX1PCI1). Xubuntu 26.04 (Linux 7.0), a different PC with a native PCI slot. `hp71` was tested on Nobara only |
| **Tested games** | ARC Raiders (7.1)<br>Squad (7.1)<br>Star Wars Jedi: Survivor (7.1)<br>Star Wars Battlefront II 2017 (7.1)<br>CMSS-3D works as expected, with much improved positional audio, as long as the game's audio output isn't set to mono or stereo/headphones. Always select System Default or Speakers as the audio output in the game's settings |
| **Kernels older than 7.0** | Untested, may not compile |
| **Audio server** | PipeWire with WirePlumber |
| **Secure Boot** | The installer signs the driver and asks you to enroll its key once at the next boot. Untested so far, please report |
| **Nobara's Easy Anti-Cheat kernel** | Can't load this driver: it only accepts modules signed by Nobara |

## You need your own Creative driver files

This project contains **no Creative driver files and no Creative DSP program**. You provide two files from Creative's own driver (`ha20x2k.sys` and `CTEXFIFX.sys`), which a script unpacks from Creative's installer, so you don't need Windows. The firmware is then built from them on your own PC, the same way [b43-fwcutter](https://wireless.docs.kernel.org/en/latest/en/users/drivers/b43.html) has long done for Broadcom Wi-Fi firmware on Linux. Please don't share the firmware it creates, because it contains Creative's code.

## Quick start

Full instructions are in **[INSTALL.md](INSTALL.md)**. In short:

1. Get this project:
   ```bash
   git clone https://github.com/Goophammer/xfi-dsp-linux
   cd xfi-dsp-linux
   ```
2. Download [Creative's X-Fi driver installer](https://support.creative.com/downloads/download.aspx?nDownloadId=100271), version 2.30.0012, and drop it into `creative/`.
3. Install the build tools and your kernel's headers.
4. Build, which also unpacks the two files it needs from the installer:
   ```bash
   ./build.sh
   ```
   This builds the default mode, `hp71`. For the stereo modes add their names, as in `./build.sh game ent`.
5. Try it. This lasts until the next reboot, then your normal driver is back:
   ```bash
   ./src/xfi_test.sh
   ```
6. Install permanently:
   ```bash
   sudo ./src/xfi_dsp_install.sh install-dkms
   ```

The test closes programs using sound devices, so read the install guide first.

Effects are in the tray app after installing, or in `alsamixer -c XFi`, where the microphone level is on the Capture page (F4).

## How it works

The card doesn't hold its effects program. Under Windows, Creative's driver loads it onto the DSP and starts it. The normal Linux driver doesn't, and nothing documents how. So the Windows driver was run in a virtual machine with the card passed through, and every register access was recorded. A small kernel addition replays that start from Linux, loading the DSP program from the firmware built from your Creative files, then runs each effect switch the same way Windows does. The DSP then runs Creative's effects just as it does under Windows.

The recipes in this project hold those hardware register values, and refer to the DSP code by location in your own Creative files. Technical notes are in [docs/TECHNICAL_NOTES.md](docs/TECHNICAL_NOTES.md).

## Good to know

- **Secure the card firmly in its slot.** A card that moves, for example when you plug into its jacks, can crash or freeze the whole PC.
- **Check your game's audio setting.** CMSS-3D needs surround from the game, so use System Default, Surround, Speakers or Auto, not Stereo, Headphones or Mono.
- **\*EAX does nothing on music or games in `hp71`, on Windows too.** In Game Mode, EAX is a reverb that games drive themselves: under Windows, a game with EAX support sends each sound into it through Creative's DirectSound3D or OpenAL drivers, and the EAX slider only sets how loud the reverb is. Music, and games without EAX support, play as one plain stream that never reaches the reverb. On Linux games have no way to use the card's EAX, but [DSOAL](https://github.com/kcat/dsoal) with OpenAL Soft recreates EAX in software for old DirectSound3D games, also under Wine and Proton, and OpenAL Soft itself does it for OpenAL games. The only thing wired into the card's reverb is your mic (Mic Environment), which is why you hear EAX through Mic Monitor.
- **Using several effects at once can cause clipping.** If it does, lower the volume.
- **Bass Boost can clip** on music that already has a lot of bass. Lower the level or the volume.
- With **What U Hear in voice chat,** other people hear everything you hear.
- **What U Hear doesn't record Bass Boost,** because Bass Boost is added after that point.

## Help wanted

If you have an X-Fi, especially a different model, please open an issue saying whether it worked, with:

- your card model and `lspci -nnv -d 1102:` output
- your distro and kernel version (`uname -r`)
- whether the card is in a native PCI slot or a PCIe-to-PCI adapter
- the mode you tried
- what worked and what didn't: sound, effects, microphone, What U Hear, sleep and resume
- `sudo dmesg | grep -iE "ctxfi|windows mode|windows path|xfi"`

## Legal

Not affiliated with or endorsed by Creative Technology Ltd. Sound Blaster, X-Fi, CMSS-3D, Crystalizer and EAX are trademarks of their owners. The driver is based on the Linux kernel's `snd-ctxfi` driver and is licensed under GPL-2.0, see [LICENSE](LICENSE). The install guide explains what the package does and doesn't contain. Use at your own risk.

---

Made possible with heavy use of Claude Code.
