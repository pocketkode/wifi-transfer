# Wi-Fi Transfer

**Move files to and from your muOS game console from any phone or PC browser**, and back up the whole SD card. No
cables, card reader or extra apps needed.

Tested on the Anbernic RG40XXH with muOS 2601.0 Jacaranda. Other muOS devices with Wi-Fi may work: reports are welcome.

![Wi-Fi Transfer on the game console](screenshots/handheld.png)

| | | |
|---|---|---|
| ![SD card tabs](screenshots/home.png) | ![A folder with upload buttons](screenshots/roms.png) | ![Select several files: download, move, copy or delete](screenshots/gba.png) |
| ![Search on both SD cards](screenshots/search.png) | ![Full backup of SD card 1](screenshots/backup-ready.png) | ![Backup in progress](screenshots/backup-running.png) |

## Install

Download Wi-Fi Transfer from [pocketkode.com](https://pocketkode.com/apps/wifi-transfer/), copy the `.muxapp` file to the `ARCHIVE`
folder on SD card 1, then open **Applications → Archive Manager** and install it. Then open **Applications → Wi-Fi Transfer**.

## How to use

1. Turn on Wi-Fi on the game console (**Configuration → Network**) and open **Applications → Wi-Fi Transfer**.
2. On your phone, **scan the QR code**, or type the address shown (e.g. `http://192.168.1.23:8080`) into a browser on a phone or PC on the **same Wi-Fi**.
3. Enter the **PIN** shown on the game console.
4. Pick a card tab. **Add games** puts each game in the right folder; tick items to download, move, copy or delete them; search finds files on both cards.
5. When you're done, press **B** on the game console. **X** keeps it running in the background instead.

The **?** button on the page opens the full guide.

## What you can do

- **SD card 1 and SD card 2** tabs show each card as it is, every folder and file with its size; the top of the page shows each card's free space.
- **Add games** and **Add a game folder** on a card's tab: each game goes to the right folder on that card (`.gba` → `ROMS/GBA`), `.muxapp` apps go to `ARCHIVE`. Inside a folder, **Upload files** and **Upload a folder** put things exactly there.
- **Search** both cards by name.
- **Select several items** to download them as one `.zip`, move or copy them (to either card), or delete them.
- **Full backup of SD card 1**: the whole card (muOS, settings, games, saves) as one `.img.gz` file. Write it to a new card with Rufus or balenaEtcher and everything is back. Keep it private: it holds your games, saves and BIOS files.
- **Background:** press **X** on the game console to go back to the muOS menu or play while the page keeps working.

Guides with pictures: [Back up and restore your muOS SD card](https://pocketkode.com/blog/backup-restore-muos-sd-card/) and
[Free blocked space on a muOS SD card](https://pocketkode.com/blog/free-blocked-space-muos-sd-card/).

## Security

- The transfer page runs only while Wi-Fi Transfer is open or running in the background, and stops by itself after **15 minutes** without activity.
- It's reachable only from your **local network** and needs a **new random PIN** each time (**Y** makes a new one and logs everyone out). **5 wrong PINs** lock that device out for a minute.
- It shows everything on your SD cards, including muOS's own files in `MUOS`: deleting, renaming or moving anything there asks first.
- The page uses plain `http://` on your home network, like most device apps. Don't run it on public Wi-Fi.

## Controls

| Button | Action |
|---|---|
| B | Stop and quit (asks again if something is still transferring) |
| X | Keep running in the background |
| Y | New PIN (logs everyone out) |
| A | Start again after an automatic stop |
| SELECT | Change the language of the game console's screen |

The game console and the transfer page come in English, 日本語, Español, Français, Deutsch, Nederlands, Русский, 简体中文 and 한국어.

## Privacy and updates

Your files go only between this game console and **your own** phone or PC. They never go over the internet, and Wi-Fi
Transfer never reads or sends them anywhere else. To check for updates it reads the latest release on **GitHub** (you can
turn this off at the bottom of the transfer page); an update you choose to install is downloaded from pocketkode.com and
checked (SHA-256 and PocketKode's signature) before anything changes. It sends nothing about you or the game console.
No ads, no analytics, no tracking. More on [pocketkode.com/privacy](https://pocketkode.com/privacy/).

## Help

Logs are in `logs/app.log` in the app folder. Bug reports and ideas: **feedback@pocketkode.com**.

## Third-party

PySDL2 (CC0 or zlib), segno (BSD 3-Clause), DejaVu Sans (Bitstream Vera Fonts license) and Noto Sans JP, SC and KR
(SIL Open Font License 1.1). Their license texts are in the app's `licenses/` folder; see THIRD-PARTY-NOTICES.md.

## License

© 2026 PocketKode. All rights reserved. Bundled third-party parts keep their own licenses (see the `licenses/` folder).
