# Technical notes

How the X-Fi (20K1) behaves in this project, for anyone working on the driver. Everything here was observed on a Sound Blaster X-Fi XtremeGamer (SB073x): from register traces of the Windows driver running in a virtual machine with the card passed through, and from tests on Linux. Statements about what is unknown are marked as such.

Addresses are register addresses in the card's register space unless noted. "Params" are slots of the parameter engine, described below.

## The approach

The Windows driver's start is recorded in a VM (VFIO passthrough, every register access traced), then replayed register by register from the Linux driver (`src/ct_winmode.c`), DSP program load included. The DSP then runs its effects as it does under Windows. The firmware file is built locally from the user's own Creative files: `src/fw_seq.py` stores DSP code and data as references into those files, and `src/fw_blob.py` builds the file.

## Replaying the start

- **Handshakes are required.** Windows polls the DSP between writes, and a blind replay with fixed 5 µs spacing loaded modules wrongly (distorted, unreliable). Two waits reproduce it: an idle poll on X memory `0x4FA9..0x4FAC` all zero (`OP_WAITZ`), and the command mailbox status words at host `0x1458/0x1468/0x1478/0x1488` = 2 (`OP_WAITEQ`). With those, 5 µs spacing is fine. More than 100 timed-out waits means a bad load, so retry. Six timeouts on a normal start are expected.
- **`0x114000–0x1149FC` must be zeroed** at start.
- **Freeze the Linux driver** while the DSP runs: otherwise it re-commits its own mixer operands into the replayed mixer program within a minute. Global interrupts off, and all of the driver's register writes dropped (`ct20k1_set_freeze`). Code that must still reach the card has to use raw writes. The freeze must be **lifted before S3 resume**, or the PLL init is dropped and the card is disabled.
- **A VM reboot doesn't reset the X-Fi**, and Windows Fast Startup resumes drivers. Both give a short driver start (288 transport registers instead of about 3900–4700) that can't be replayed. Reset the card before every traced boot, turn Fast Startup off, and end sessions with `shutdown /s /t 0`.

## Transport, page tables and the tank

- Windows registers 16 page-table pages: 32 MB of device addresses, 4 KB pages, 64-bit entries. Transport map entries are `[device address, physHi, physLo]`. At runtime, word 0 of a stream's entry holds its current device address.
- **The tank (DSP delay-line memory, device `0x1000–0x1FFFFF`) lives in the card's own memory:** the page tables map device address D to **BAR3 + D − 0x1000**. With the tank in host RAM instead, on a system with the card behind a PCIe-to-PCI bridge, Game mode (2×) sounded fine but **Entertainment mode (4×) was distorted**, and the tank looked starved: tank entries `0x110000+` got bit 31 set, and the queue count `0x13B400` stayed at 16 (full) where Windows shows 3. Moving the tank to BAR3 fixed it. Host RAM wasn't tried in a native PCI slot, so the bridge's part in this is unproven.
- In a VM the guest writes guest BAR3 addresses there, so traced sessions never have a working tank. Page-table contents never appear in a register trace.
- Device memory `0x1B70000–0x1B7FFFF` is DSP working memory. Windows' stereo playback loop is `0x1B7D000–0x1B7EE00` (960 frames, S32), and recording goes to `0x1B55000–0x1B56E00`.

## Modes and sample rate

- **Entertainment mode runs the card at 4× (192 kHz internal), Game mode at 2×.** PLL `0x1C6060` = `0x1480B001` (4×) or `0x1480A001` (2×); I2S `0x1C5420` low bits 3 or 2; rate fields in DAIO `0x1C54D0/D4`.
- **The DAC must be told over I²C**, which a register trace of the BAR doesn't show (region 0, index `0x10`, data `0x14`, device `0x180080`): reg `0x01` = `0xA0` (4×) or `0x80`, regs `0x06/0x09/0x0C/0x0F` = `0x26` (4×) or `0x25` (2×). Without it: noise.
- Speaker setup changes **reload DSP modules and rearrange the layout**, and so does EAX in some setups. Effect bursts recorded after such a change don't fit a clean start, so a firmware has to come from one session in the target setup. The analog input section can differ between Windows sessions. In Game mode with 7.1 + Headphones, the EAX slider and Mic Environment are only parameters plus 8 mixer words.
- Separate sessions in the same setup can match: the 7.1 + Headphones starts had identical mixer, converter, ring and DAIO state, so bursts from later sessions fit the first one.

## Parameter engine

- Parameter slot p: PMOP `0x148000+4p`, current value PCURR `0x150000+4p`, target PTRAG `0x154000+4p`, slope PSR `0x158000+4p`. Values are 8.24 fixed point, 1.0 = `0x01000000`.
- **The write pattern that takes effect: PTRAG=x, PSR=0, PCURR=x.** PTRAG and PSR alone did nothing. These registers read back 0.
- A start state may hold a ramp in progress: target 1.0 with current 0 means the value settles at 1.0.
- Values are delivered to DSP core 0's parameter area at host `0x100000 + 4·local`, with positions allocated per load.

## Volume and effects

- **Master volume:** params `0x0E,0x0F,0x12,0x13,0x16,0x17,0x1A,0x1B,0x1E,0x1F,0x22,0x23,0x26,0x27` (1.0; `0x17/0x27` at 0.5), applied after the effects, plus a DSP float of 0.1/volume that tells the effects the level (X `0x10141C` Game, `0x1012D8` Entertainment). Scaling samples before the DSP instead made SVM over-compensate.
- **Mic monitor:** params `0xAE/0xAF`. **Recording level:** params `0x42/0x43` (0.5 = Windows' 51, about 2.0 = 100).
- **Crystalizer:** one on/off float word (`0x1DB4` Game, `0xBE58` Entertainment), followed by 17 coefficient words for intensity.
- **EQ (Game mode):** params `0x1A8+8k` (k = 0..7) fit 8 RBJ peaking biquads at octave centres, 4 coefficients `(a2, a1, b1, b2)`, numerator normalised by b0, 8.24. `0x1E8–0x1ED` fit an RBJ high shelf, `0x1F4` is the EQ level and `0x2E4` overall gain. Windows' preset bursts leave out what didn't change from the previous preset, so a replayed preset has to be made absolute. Each preset carries its own level.
- **Bass Boost:** Windows' on and off bursts are parameters and rings only, with no mixer writes, and repeat identically. On sets params `0x23B…0x243` from 0 to `0x2D8622` (0.18), which sends five of seven channel groups to buses `0x86/0x87`. It's clearly audible in headphones but measures 0.0 dB on What U Hear, so it joins after that tap. Replaying only its slider steps, without the bursts, changed the sound but not the bass. The level and cutoff values are 4 cascaded biquads plus a low-pass; the shelf doesn't fit RBJ formulas, so arbitrary settings can't be computed yet. Windows moves the level in 0.5 dB passes about 100 ms apart, each pass writing 70 coefficients at `0x198000…0x19BFFF` and zeros at `0x149000…0x14FFFF`. The driver replays those passes one at a time, only while Bass Boost is on, because a single jump is audible. The steps run in the background, so a slide doesn't hold up other control changes. Going back to a level restores exactly the same values, in both directions. The cutoff changes 90 coefficients, but was only traced at 6 dB, so other combinations aren't available.
- **CMSS-3D in 2/2.1 has no output path:** on/off only flips parameter words. A 4.1 setup rebuilds the graph (about 1100 mixer operands).

## 7.1 input and CMSS-3D Headphone (mode `hp71`)

- **8-lane stream:** converters 202–209, control `0x1708CE`, partners `0x1008C0`, CCR `0x2000400`, CA +`0x400`, loop `0x1B7D000–0x1B84800` (960 frames × 8 × 4 bytes). PCM order FL FR FC LFE BL BR SL SR.
- **CMSS-3D Headphone switches with mixer entries and parameters only.** MacroFX and Elevation Filter wrote nothing.
- **Windows moves forward through three op banks and never goes back:** start chain ops `0x92C…`, CMSS on `0x4F1…`, CMSS off `0x55D…`. Each burst builds the next bank and clears the previous one, so the bursts aren't inverses. Replaying on after off left two chains live, far too loud. Writing the start state back on off muted the output, cause unknown. What works: off = Windows' burst, on = clear what only off writes, then Windows' on.
- **The start must be cut after the music stream**, not the startup stream: CMSS on rewrites the mixer slots of the stream that is playing.
- **Three parameter banks of 41, with the same destinations:** start `0x273…`, on `0x2EA…`, off `0x33B…`. Controls recorded in one bank must write the matching parameter in all three. A CMSS switch resets them, so the driver re-applies them.
- **EAX slider:** params `0x13E…` follow it (-60 to +6 dB, `0xA8` = off); `0x36F…` follow it above 0 dB only. No effect on music.
- **Mic Environment:** wet `0x38E/0x38F` rises over steps 1–5, dry `0x367/0x36A` falls over steps 6–10. The first step links the mic into the effect.
- **Bass with CMSS on** (tone in one channel at -12 dB): on its own channel, LFE reaches both ears at -15 dB, flat to 1 kHz, with CMSS on or off (see the next note for `hp71` now). Below about 200 Hz every other single channel goes to both ears at about -6 dB each. Bass in both front channels keeps its level (-18 dB in, -18 dB out). So positional bass sounds lighter, especially in a game that sends nothing on LFE.
- **LFE folds into the fronts:** with CMSS on or off, LFE is added to front left and right at +5.5 dB relative to a front channel on Windows, so with CMSS on it takes the fronts' curve. It's unclear whether Windows or Creative's driver does this. Left on its own channel it skips CMSS and reaches both ears 8.5 dB lower. In `hp71` the driver folds it while copying into the loop. Measured on What U Hear with a 7.1 test file, the result is within about 0.4 dB of Windows. With CMSS on, FC is still about 2 dB lower than on Windows, mostly below 1 kHz.
- **Stereo gets extra processing on Windows:** with CMSS on, stereo input gets ±47 Hz side frequencies (48000/1024), different left and right from identical input, and slow settling. Real 7.1 input doesn't, and the DSP gets no parameter writes during it. It only happens with CMSS on, so it's probably Creative's software, not Windows itself, but that isn't proven. Linux doesn't do this.
- The Center/Subwoofer jack stayed silent in `game` and `hp71`.

## Recording

- **Path:** ADC rings `0x144/0x146` and `0x149/0x14E` → mixer → two rate converters (F32, pitch 2.0 at 2×, 4.0 at 4×) → mixer → one memory-writing converter (S32) → host loop, 2 channels. Converters: 75 (+14/15) in `hp71`, 75 (+76/77) in `game`, 157 (+30/31) in `ent`.
- **Mic channels in `hp71`:** the left loop channel is an exact copy of the right one, 12 dB lower (coherence 1.00 in every band), and doesn't follow the recording level (params `0x42/0x43`). The right one does, and the ADC gain moves both. Windows' start leaves `0x42` at +6 dB and `0x43` heading to -6 dB, but writing `0x43` doesn't bring the left up, while What U Hear, through the same level ops, records balanced. Where the left loses its level is not known. The mic is mono, so while recording it the driver sends the right channel to both sides (`winrec_mono`). In `game` the first test (2026-10-30, both params at -6 dB) showed L = R.
- **Stay behind the converter.** Copying up to its reported position took stale frames from one loop earlier on every 2 ms tick: an "old tape" sound, sidebands at ±500 Hz multiples at -38 dB on a 1037 Hz tone. Staying 64 frames behind (`winrec_behind`) brought it below -92 dB. **Test with frequencies that don't fit the loop:** 1000 and 1500 Hz repeat with the 960-frame loop and hide this fault completely.
- **The converter's position is odd at the wrap:** for about 40 µs per loop it reads as a bare offset (`0x0`, `0x20`) on playback, or exactly the loop's end on the recording side. Both mean the start of the loop. A 2 ms tick lines up with that window for about 1.4 s every 70 s, as the card and PC clocks drift. Counting those as bad made the recording resync and lose about a quarter of a second each time.
- **Input select isn't in the replay.** Windows ends with ADC `0x1A0080` reg `0x2B` = 1, reg `0x1C` = `0xCF`, GPIO7 on (`0x1C6020`), direction `0xFE` (`0x1C6024`). Linux left GPIO7 off, recording silence. The driver now does this after the start, for SB073x only. That and a jack replug happened in one test, so which one mattered isn't proven.
- **Mic gain is ADC reg `0x1C`** (device `0x1A0080`), in 0.5 dB steps: `0xCF` = 0 dB, `0xE7` = +12 dB and `0xF7` = +20 dB, as in the stock driver. It sits before the DSP and moves both recording channels, Mic Monitor and the noise equally. "DSP Mic Boost" (0 to +20 dB) writes it after every start and resume. Windows only has a +20 dB toggle, and with it off it records louder than Linux at 0 dB. Its no-boost value isn't known, because I²C writes aren't in the traces.
- **Jack sense is in `0x1C6020`:** bit 11 clears when headphones are in the front panel header, bit 15 sets with a plug in the rear green jack, and bit 14 sets with a plug in the next rear jack. Clearing bit 6 silences the rear jacks while the front panel keeps playing, with no pop. The driver also clears it during the DSP start and sets it 0.5 s after: at boot that removed the hiss and most of the pops on the rear jacks. Windows' "automatically mute speakers" polls these about 3 times a second, flips bit 6 and zeroes 12 Master channel parameters (`0x12…0x27`). The driver only flips bit 6, which was enough on test. Windows' "automatically enable headphone settings" switches from the current speaker setup to Headphones on plug-in, and back on unplug. Traced with 2/2.1, it's the same size of job as switching by hand: about 2 s and 42,000 writes, most of them reloading DSP data. Repeating the same switch doesn't end in the same addresses, so unlike the effect bursts it can't be replayed as a fixed list.
- **The recording stays dry by routing:** the mic splits at ops `0x3B4…` into a recording branch (`0x314…`, then level ops `0x2C4…`) and a monitor branch (`0x58E…`). Mic Environment links in after the monitor, so it needs Mic Monitor on and doesn't reach the recording converter.
- **What U Hear:** Windows points the level ops `0x2C4…` at `0x8F9…` (gains `0x33F/0x342`) instead of the mic branch. Windows' source `0x82C…` feeds master params `0x1E/0x1F`, so it's before the master volume. Music is on all five master sources at the same level, but the voice with Mic Environment is at -21 dB on `0x8A9…` and -41 dB on `0x82C…`, so `hp71` uses `0x8A9…`. At gain 2.0 the mix clips; 1.0 is safe.

## Linux side

- PipeWire reads the card's mixer controls once. The firmware's controls appear after the DSP starts, so the autostart restarts WirePlumber once it's up.
- ACP drops a mapping if the device is busy when it probes, and if its channel map doesn't match. When testing a profile set with `spa-acp-tool`, the card has to be idle.
- If something opens device 7 directly while PipeWire tries to, PipeWire can lose the source. Switching the card profile off and on brings it back.
- **PipeWire keeps a stale software volume after What U Hear.** The What U Hear port uses software volume. Back on the mic port, PipeWire moves the volume to the card's level control but keeps the old software volume. Discord's auto gain had left it at -29 dB, and the mic seemed dead. Setting the volume through PipeWire doesn't clear it; only a change event on "DSP Mic Capture Volume" does, so the tray moves the level one step and back after switching to the mic. Check with `pw-dump`, `Props` `softVolumes` of the source node.
- Selecting the same profile again does nothing, so `src/xfi_profile.sh` switches off first when the source is missing.
- DKMS saves the module it finds as the "original". A copy left in `updates/` gets saved instead of the distro's module, which then stays in `kernel/` and wins at boot. The installer removes that copy before running DKMS.
- A kernel that enforces module signatures with its own key (Nobara's Easy Anti-Cheat kernel) can't load this driver.

## Hardware behaviour

- **Some register reads have side effects:** reading the whole register space breaks the DSP output until reload. Read single registers only.
- **A card that moves in its slot can stop answering:** every read returns `0xFFFFFFFF`, and each read can stall the CPU for milliseconds, so a timer loop can freeze the PC. The driver stops touching a card in this state.

## Open questions

- How does audio reach the rear, side and subwoofer outputs? The subwoofer is the orange jack. Dumps from real Windows show the 4.1 setup rebuilds the mixer (about 1,100 words) and 90 DSP inputs, and the rears reuse existing DAC channels. Needs a traced start in a speaker setup.
- Where does the left mic channel lose its level in `hp71` (see Recording)? What U Hear through the same level ops is balanced.
- The front panel mic header records about 24 dB weaker than the rear jack, with the same mic and settings. Is it wired to another input, or does it need its own select?
- During startup, what opens the normal sound device and causes "PLL initialization failed", and what are the 6 waits that time out at op 296131? Both look harmless.
