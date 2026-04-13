# Linux VPS Setup Guide

Panduan lengkap menjalankan **trade-bot** di VPS Linux tanpa Windows.

## Arsitektur

```
Linux VPS
┌─────────────────────────────────────────────────────┐
│  Python Bot (native Linux)                          │
│  import mt5linux as mt5  ← API identik dengan MT5  │
│       │ RPyC socket (localhost:18812)               │
│       ▼                                             │
│  Wine → MT5.exe + Wine Python + MetaTrader5 pkg     │
└─────────────────────────────────────────────────────┘
```

---

## Step 1: Install Wine + MT5 (Official MetaQuotes Script)

```bash
# Supported: Ubuntu 20.04+, Debian 11+, Linux Mint, Fedora
# JANGAN jalankan dengan sudo
wget https://download.terminal.free/cdn/web/metaquotes.software.corp/mt5/mt5linux.sh
chmod +x mt5linux.sh
./mt5linux.sh
```

Script ini akan otomatis:
- Detect distro Linux kamu
- Install Wine yang sesuai
- Download dan install MT5 terminal

Setelah selesai, **restart** sistem:
```bash
sudo reboot
```

---

## Step 2: Install Wine Python + MetaTrader5 Package

Setelah Wine terinstall, install Python for Windows di dalam Wine environment:

```bash
# Download Python Windows installer
wget https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe

# Install Python di Wine (ikuti wizard, centang "Add to PATH")
wine python-3.11.9-amd64.exe

# Install MetaTrader5 dan mt5linux di Wine Python
wine pip install MetaTrader5 mt5linux
```

---

## Step 3: Install Bot Dependencies (Linux Python)

```bash
# Clone atau copy bot ke VPS
cd /opt/trade-bot

# Buat virtualenv
python3 -m venv venv
source venv/bin/activate

# Install dependencies (mt5linux akan otomatis terinstall karena sys_platform != win32)
pip install -r trading_bot/requirements.txt
```

---

## Step 4: Konfigurasi MT5

1. Buka MT5 di Wine:
   ```bash
   ~/.mt5/drive_c/Program\ Files/MetaTrader\ 5/terminal64.exe &
   ```
2. Login ke akun broker kamu
3. Pastikan "Allow Algo Trading" aktif (toolbar hijau)
4. Biarkan MT5 tetap berjalan (bisa headless)

---

## Step 5: Start Bridge Server

Bridge server harus berjalan SEBELUM bot Python:

```bash
# Start mt5linux bridge (foreground untuk test)
wine python -m mt5linux

# Untuk background:
wine python -m mt5linux &

# Atau dengan nohup agar tetap berjalan setelah SSH disconnect:
nohup wine python -m mt5linux > /tmp/mt5_bridge.log 2>&1 &
```

Bridge akan listen di `localhost:18812` (default RPyC port).

---

## Step 6: Jalankan Bot

```bash
cd /opt/trade-bot
source venv/bin/activate

# Test koneksi MT5 dulu
python -c "import mt5linux as mt5; mt5.initialize(); print(mt5.account_info())"

# Jalankan bot
python trading_bot/main.py --mode live
```

---

## Step 7: Setup Systemd Service (Agar Auto-Start)

Buat 2 service: satu untuk bridge, satu untuk bot.

### `/etc/systemd/system/mt5-bridge.service`

```ini
[Unit]
Description=MT5 Linux Wine Bridge
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME
ExecStart=/usr/bin/wine python -m mt5linux
Restart=always
RestartSec=10
Environment=DISPLAY=:0
Environment=WINEPREFIX=/home/YOUR_USERNAME/.wine

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/trade-bot.service`

```ini
[Unit]
Description=Trade Bot Python
After=mt5-bridge.service
Requires=mt5-bridge.service

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/opt/trade-bot
ExecStart=/opt/trade-bot/venv/bin/python trading_bot/main.py --mode live
Restart=always
RestartSec=30
EnvironmentFile=/opt/trade-bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
# Enable dan start service
sudo systemctl daemon-reload
sudo systemctl enable mt5-bridge trade-bot
sudo systemctl start mt5-bridge
sleep 5  # tunggu bridge ready
sudo systemctl start trade-bot

# Cek status
sudo systemctl status trade-bot
journalctl -u trade-bot -f  # live logs
```

---

## Troubleshooting

| Problem | Solusi |
|---------|--------|
| `ImportError: mt5linux` | `pip install mt5linux` di Linux Python |
| Bridge tidak konek | Pastikan `wine python -m mt5linux` berjalan dahulu |
| MT5 tidak mau login | Buka MT5 manual via Wine, login manual sekali |
| `DISPLAY` error di Wine | Set `export DISPLAY=:0` atau gunakan Xvfb (headless) |
| Chart rendering error | `pip install mplfinance matplotlib` |

### Headless Mode (Tanpa Monitor / VPS Tanpa GUI)

```bash
# Install Xvfb untuk virtual display
sudo apt install xvfb

# Start virtual display
Xvfb :1 -screen 0 1024x768x24 &
export DISPLAY=:1

# Sekarang Wine bisa jalan tanpa monitor
wine python -m mt5linux &
```

---

## Catatan Penting

- **MT5 tetap harus login** ke broker — tidak bisa fully headless untuk koneksi broker
- **Bridge server harus berjalan** sebelum bot Python distart
- Di Linux, semua fitur bot tersedia: live trading, fibo engine, chart ke Telegram
- `mplfinance` + `matplotlib` jalan sepenuhnya di Linux (pure Python, tidak butuh GUI)
