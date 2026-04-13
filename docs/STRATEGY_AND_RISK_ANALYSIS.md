# 🧠 Blueprint Strategi & Manajemen Risiko XAUUSD Bot

Dokumen ini merangkum seluruh arsitektur *Trading Engine* yang tertanam pada bot, mencakup metode entry, mekanisme exit, serta protokol *Risk Management* tingkat tinggi yang beroperasi secara otomatis.

---

## 1. ⚙️ Trading Engines (Metode Entry & Analisa)

Bot ini beroperasi menggunakan 4 mesin (engine) hibrida yang berjalan secara paralel. Masing-masing dirancang untuk menangkap kondisi pasar yang berbeda.

### A. ⚡ Pulse Scalper (`pulse_engine.py`)
*   **Kondisi Pasar:** Volatilitas tinggi, pergerakan impulsif jangka pendek (M1).
*   **Konsep:** Micro-scalping berfrekuensi tinggi.
*   **Metode Entry:** Mendeteksi *momentum burst*/lonjakan harga tiba-tiba di timeframe 1 Menit.
*   **Target Profit (TP):** Sangat tipis. Secara otomatis akan *Take Profit* di kisaran **$1 - $5** untuk keluar pasar secepat mungkin sebelum harga berbalik.
*   **Karakteristik Risiko:** Sangat rentan terhadap *whipsaw* (pergerakan bolak-balik cepat).

### B. 🔄 Reversal Engine (`reversal_engine.py`)
*   **Kondisi Pasar:** *Over-extended* (harga sudah bergerak terlalu jauh searah) atau *exhaustion*.
*   **Konsep:** *Mean-reversion* / Mencari titik puncak (Peak) atau lembah (Trough).
*   **Metode Entry:** 
    *   **MACD Histogram Acceleration:** Mencari penyusutan histogram MACD sebanyak 3 kali berturut-turut di zona ekstrem (indikasi momentum pelemahan).
    *   **Retest & Divergence:** Memanfaatkan divergensi teknikal dan *rejection wicks* (ekor panjang) di M1/M5.
*   **Karakteristik Risiko:** Mengandung risiko menangkap pisau jatuh (*catching a falling knife*). Dilindungi oleh filter konfirmasi *candlestick* M1 sebelum eksekusi.

### C. 📐 Fibonacci MTF Engine (`fibo_engine.py`)
*   **Kondisi Pasar:** Trending yang sehat (*pullback & continuation*).
*   **Konsep:** Integrasi Multi-Timeframe (MTF) menggunakan area *Golden Ratio* Fibonacci.
*   **Metode Entry:** 
    *   **Context (M15):** Menentukan arah Tren Mayor dan menarik garis Fibonacci dari *Swing Low* ke *Swing High* (atau sebaliknya).
    *   **Gate (M5):** Harga harus masuk zona retracement **50% - 78.6%**. Divalidasi oleh **RSI Strict Bounce** (RSI harus menyentuh *oversold/overbought* lalu memantul keluar, bukan sekadar lewat).
    *   **Trigger (M1):** Menunggu *Engulfing Candle* atau *Pin Bar* yang solid sebagai pemicu tembak.
*   **Karakteristik Risiko:** Sangat terstruktur. Menggunakan sistem **Tiering** (Tier A = Full Lot, Tier B = Half Lot).

### D. 🛡️ ICT / SMC Standard Engine (`advanced_signal_engine.py`)
*   **Kondisi Pasar:** Netral ke Trending.
*   **Konsep:** *Smart Money Concepts* (SMC) dipadukan dengan Confluence Multi-Timeframe Indicator.
*   **Metode Entry:** 
    *   Sistem *Scoring* dinamis (maksimal skor base + bonus).
    *   Mencari *Fair Value Gaps* (FVG), *Order Blocks* (OB), dan *Liquidity Sweeps* (ChoCH/BOS).
    *   Didukung oleh pembobotan indikator klasik: EMA Crossovers, ADX untuk kekuatan tren, dan Bollinger Bands.
*   **Karakteristik Risiko:** Moderat. Ini adalah tulang punggung bot untuk *swing* intraday. Keputusan akhir sering kali difilter ulang oleh modul *AI Scorer*.

### E. 🔥 Velocity Breakout Engine (`velocity_engine.py`)
*   **Kondisi Pasar:** Ledakan volatilitas pembukaan (Market Open).
*   **Konsep:** *Momentum Burst / Opening Range Breakout (ORB).*
*   **Metode Entry:** 
    *   Hanya hidup persis 1 Jam saat pembukaan bursa London dan New York.
    *   Mencari breakout di luar harga *sideways/konsolidasi* M1 sebelum sesi buka, dibarengi volume paku naik di atas ADX 25.
*   **Karakteristik Risiko:** SL Ultra Tipis (cuma di buntut candle M1 penembus). TP cepat 1:2. Sangat efisien, bebas dari *floating* lama.

---

## 2. 🆔 Signal ID Naming Conventions

Setiap sinyal yang dieksekusi oleh bot memiliki ID unik yang menunjukkan *engine* mana yang memicunya. Struktur penamaannya adalah sebagai berikut:

| Prefix | Engine | Contoh | Keterangan |
| :--- | :--- | :--- | :--- |
| **`SMC_`** *(sebelumnya `ALPHA_`)* | ICT SMC Standard | `SMC_1776047756` | Break of Structure, eksekusi Trend Continuation dengan FVG/OB. |
| **`REV_`** | Reversal | `REV_6A7B8C` | Eksekusi apex/trough saat ekstensi ekstrem (MACD Divergence). |
| **`FIBO-`** | Fibonacci MTF | `FIBO-A1B2C3` | Pantulan harga di area retracement Fibonacci (50-78.6%) plus RS strict. |
| **`PULSE_`** | Pulse Scalper | `PULSE_X9Y8Z7` | Sambaran momentum cepat di M1 dengan auto take profit tipis. |
| **`VELO_`** | Velocity Burst | `VELO_M8N7C2` | Menunggang volatilitas saat Market Open (London/NY) dengan SL ultra tipis. |

---

## 3. 🛡️ Risk Management Protocol (Manajemen Risiko)

Semua sinyal dari ke-4 engine di atas **TIDAK AKAN** dieksekusi sebelum melewati "Gerbang Risiko" (Risk Executor & Filters). Mekanisme ini dirancang khusus untuk melindungi *Equity*.

### A. Pre-Trade Filters (`filters.py`)
Setiap sinyal harus lulus daftar periksa berikut agar tidak ditolak (*rejected*):
1.  **Weekly Open Filter:** Larangan melakukan BUY jika harga berada di bawah pembukaan harga mingguan (*bearish bias*), dan sebaliknya untuk SELL.
2.  **Daily Exhaustion (Kelelahan Ekstrim):** Jika Gold (XAUUSD) sudah berlari lebih dari **330 pips** searah dalam satu hari, bot akan otomatis menolak entry *follow-trend* karena probabilitas pembalikan arah (*mean-reversion*) membesar tajam.
3.  **Choppiness Index:** Menolak entry jika rasio pergerakan harian berbanding ATR terlalu rendah (pasar sedang *sideways* / rangey).
4.  **Spread & Volatility Gate:** Tolak jika *spread broker* sedang melebar tajam (misal saat rilis berita) atau volatilitas (ATR) terlalu liar/terlalu mati.
5.  **Multi-Entry Delay (Adaptive):** Mencegah bot membuka posisi beruntun (spam) yang bisa menyebabkan Margin Call. Tergantung pada "Trading Mode", bot akan memaksa jeda (misal: 1 - 3 menit) antar trade. Jika harga volatil (ATR > 25 pips), jeda ditambah otomatis +1 menit.

### B. Position Sizing & Mode Trading
Besaran *Lot/Risk* per posisi ditentukan oleh **Mode Trading** yang dapat diubah via Telegram:
*   **Conservative / Moderate:** Risiko per trade sangat kecil (misal 0.5% - 1% modal). Target RR (Risk to Reward) lebih ketat.
*   **Aggressive / Very Aggressive:** Recovery multiplier diaktifkan (mirip *martingale* cerdas tapi dengan batas *cap*), merisikokan persentase modal lebih besar.
*   **Ultra Scalper:** Mode barbar untuk Pulse Engine, mengincar banyak entry dengan take profit kecil.

### C. Staged Trailing Stop-Loss
Terutama digunakan pada Fibonacci MTF, exit tidak dilakukan secara kaku 100%, melainkan bertahap (*Partial Close*):
*   **TP1 Hit:** Tutup 50% lot, pindahkan SL ke *Breakeven* (Balik Modal). Sisa trade berstatus *Risk-Free*.
*   **TP2 Hit:** Tutup 25% lot, pindahkan SL mengunci profit di level TP1.
*   **TP3 / Extension:** Sisa 25% dijalankan (ride) hingga ke level *Fibo Extension 161.8%* atau dibiarkan ditutup oleh *trailing SL* dinamis.

### D. Hard Safety & Loss Guards (`bot.py` & `portfolio.py`)
Mekanisme pengaman *Darurat Paling Akhir*:
1.  **Consecutive Loss Pause:** Jika bot kalah (Hit SL) 3x berturut-turut, bot akan **mematikan diri (PAUSE) selama 60 menit** untuk mendinginkan mesin dan mencegah balas dendam (*Anti-Revenge*). Selama fase pemulihan, perhitungan risiko (Risk %) dipotong menjadi 50%-75%.
2.  **Daily Loss Trigger:** Jika dalam hari tersebut total kerugian kumulatif mencapai persentase tertentu (misal 3%), bot akan otomatis PAUSE.
3.  **Global Cut Loss (Panic Close):** Sistem pertahanan saat *market* bertingkah tidak wajar. Fitur ini akan membuang (menutup) paksa seluruh posisi, namun **tetap membiarkan bot hidup** untuk mencari pergerakan yang valid setelahnya. Anda bisa mengaktifkannya via `/cutloss` dan mereset masa tunggu masuk pasar via `/reset`.
4.  **Profit Lock:** Mencegah buka posisi baru (Averaging) jika posisi yang sudah ada sedang dalam kerugian mengambang (*floating loss*) yang lumayan besar (di atas ambang batas $10).
