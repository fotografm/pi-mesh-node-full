// sdr-ws/src/main.rs
//
// RTL-SDR spectrum + waterfall server for Raspberry Pi Zero 2W.
// Cross-compiled on desktop: cargo build --release --target arm-unknown-linux-musleabihf
// Deploy:                    scp target/arm-unknown-linux-musleabihf/release/sdr-ws user@<pi>:~
//
// Ports:
//   8080 — HTTP: serves the embedded HTML waterfall page
//   8081 — WebSocket: binary spectrum frames + JSON text control messages
//
// Binary frame format (little-endian):
//   2056 bytes = 8-byte header + 2048-byte payload
//   Header:  4 × i16 = [spec_min_db×10, spec_max_db×10, wf_min_db×10, wf_max_db×10]
//   Payload: 1024 × i16 = FFT bins dBFS×10, DC at index 512
//
// Profiles stored in: ./profiles.json  (same directory as binary)

use std::collections::HashMap;
use std::f32::consts::PI;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use num_complex::Complex;
use rtl_sdr_rs::{RtlSdr, TunerGain};
use rustfft::{Fft, FftPlanner};
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::broadcast;
use tokio_tungstenite::accept_async;
use tokio_tungstenite::tungstenite::Message;

// ── Compile-time constants ────────────────────────────────────────────────────
const FFT_SIZE: usize      = 2048;
const FFT_AVERAGES: usize  = 8;
const OUTPUT_BINS: usize   = 1024;
const HTTP_PORT: u16       = 8080;
const WS_PORT: u16         = 8081;
const BROADCAST_CAP: usize = 8;
const SEND_INTERVAL_MS: u64 = 40;        // 25 fps
const PROFILES_PATH: &str  = "./profiles.json";

// ── Embedded HTML page ────────────────────────────────────────────────────────
const HTML: &str = include_str!("index.html");

// ── SDR parameters (user-adjustable, saved in profiles) ──────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SdrParams {
    centre_freq_mhz: f64,
    sample_rate_mhz: f64,
    gain_db:         f64,
    spec_min_db:     f32,
    spec_max_db:     f32,
    wf_min_db:       f32,
    wf_max_db:       f32,
}

impl Default for SdrParams {
    fn default() -> Self {
        Self {
            centre_freq_mhz: 869.525,
            sample_rate_mhz: 2.0,
            gain_db:         33.8,
            spec_min_db:    -100.0,
            spec_max_db:    -50.0,
            wf_min_db:      -95.0,
            wf_max_db:      -65.0,
        }
    }
}

impl SdrParams {
    fn centre_freq(&self) -> u32 { (self.centre_freq_mhz * 1e6) as u32 }
    fn sample_rate(&self) -> u32 { (self.sample_rate_mhz * 1e6) as u32 }
    fn gain_tenths(&self) -> i32 { (self.gain_db * 10.0).round() as i32 }
}

// ── Profiles (persisted to disk) ──────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Profiles {
    last_used: String,
    presets:   HashMap<String, SdrParams>,
}

impl Default for Profiles {
    fn default() -> Self {
        let mut presets = HashMap::new();
        presets.insert("default".to_string(), SdrParams::default());
        Self { last_used: "default".to_string(), presets }
    }
}

fn load_profiles() -> Profiles {
    if let Ok(data) = std::fs::read_to_string(PROFILES_PATH) {
        if let Ok(p) = serde_json::from_str::<Profiles>(&data) {
            return p;
        }
        eprintln!("[CFG] Could not parse {}, using defaults", PROFILES_PATH);
    }
    let p = Profiles::default();
    save_profiles(&p);
    p
}

fn save_profiles(profiles: &Profiles) {
    match serde_json::to_string_pretty(profiles) {
        Ok(json) => {
            if let Err(e) = std::fs::write(PROFILES_PATH, json) {
                eprintln!("[CFG] Could not write {}: {}", PROFILES_PATH, e);
            }
        }
        Err(e) => eprintln!("[CFG] Serialise error: {}", e),
    }
}

// ── Shared running config ─────────────────────────────────────────────────────

struct RunningConfig {
    params:  SdrParams,
    changed: bool,      // set true when params updated; cleared by SDR thread
}

// ── FFT computation ───────────────────────────────────────────────────────────

fn compute_spectrum(
    raw: &[i16],
    fft: &std::sync::Arc<dyn Fft<f32>>,
    window: &[f32],
    window_power: f32,
) -> Vec<f32> {
    let mut power = vec![0.0f32; FFT_SIZE];

    for avg in 0..FFT_AVERAGES {
        let base = avg * FFT_SIZE * 2;
        let mut buf: Vec<Complex<f32>> = (0..FFT_SIZE)
            .map(|i| Complex::new(
                raw[base + i * 2    ] as f32 / 32768.0,
                raw[base + i * 2 + 1] as f32 / 32768.0,
            ))
            .collect();

        let mean = buf.iter().fold(Complex::new(0.0f32, 0.0), |a, &b| a + b)
            * (1.0 / FFT_SIZE as f32);
        for s in buf.iter_mut() { *s -= mean; }
        for (s, &w) in buf.iter_mut().zip(window.iter()) { *s *= w; }
        fft.process(&mut buf);
        for (i, c) in buf.iter().enumerate() {
            power[i] += c.norm_sqr() / window_power;
        }
    }

    let inv_avg = 1.0 / FFT_AVERAGES as f32;
    for p in power.iter_mut() { *p *= inv_avg; }

    let power_db: Vec<f32> = power.iter()
        .map(|&p| 10.0 * (p + 1e-12_f32).log10())
        .collect();

    let half = FFT_SIZE / 2;
    let mut shifted = vec![0.0f32; FFT_SIZE];
    shifted[..half].copy_from_slice(&power_db[half..]);
    shifted[half..].copy_from_slice(&power_db[..half]);
    shifted
}

fn encode_frame(bins: &[f32], params: &SdrParams) -> Vec<u8> {
    let step = FFT_SIZE / OUTPUT_BINS;
    let mut out = Vec::with_capacity(8 + OUTPUT_BINS * 2);

    for &val in &[params.spec_min_db, params.spec_max_db, params.wf_min_db, params.wf_max_db] {
        let v = (val * 10.0).round().clamp(i16::MIN as f32, i16::MAX as f32) as i16;
        out.extend_from_slice(&v.to_le_bytes());
    }
    for chunk in bins.chunks(step) {
        let avg = chunk.iter().sum::<f32>() / chunk.len() as f32;
        let v = (avg * 10.0).round().clamp(i16::MIN as f32, i16::MAX as f32) as i16;
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

// ── SDR reader thread ─────────────────────────────────────────────────────────

fn sdr_reader(
    spectrum_tx: broadcast::Sender<Vec<u8>>,
    config: Arc<Mutex<RunningConfig>>,
) {
    let initial = config.lock().unwrap().params.clone();

    let mut sdr = match RtlSdr::open_with_index(0) {
        Ok(s)  => s,
        Err(e) => { eprintln!("[SDR] Failed to open: {:?}", e); return; }
    };

    sdr.set_tuner_gain(TunerGain::Manual(initial.gain_tenths())).expect("[SDR] gain");
    sdr.reset_buffer().expect("[SDR] reset");
    sdr.set_center_freq(initial.centre_freq()).expect("[SDR] freq");
    sdr.set_sample_rate(initial.sample_rate()).expect("[SDR] rate");

    eprintln!("[SDR] Ready — {:.3} MHz  BW {:.3} MHz  Gain {:.1} dB",
        initial.centre_freq_mhz, initial.sample_rate_mhz, initial.gain_db);

    let window: Vec<f32> = (0..FFT_SIZE)
        .map(|i| 0.5 * (1.0 - (2.0 * PI * i as f32 / (FFT_SIZE - 1) as f32).cos()))
        .collect();
    let window_power: f32 = window.iter().map(|w| w * w).sum();

    let mut planner = FftPlanner::<f32>::new();
    let fft = planner.plan_fft_forward(FFT_SIZE);

    let n_bytes = FFT_SIZE * FFT_AVERAGES * 2;
    let mut buf_u8 = vec![0u8; n_bytes];

    let interval  = Duration::from_millis(SEND_INTERVAL_MS);
    let mut last_send = Instant::now() - interval;
    let mut current   = initial;

    loop {
        // Check for config changes — apply to hardware if needed
        {
            let mut cfg = config.lock().unwrap();
            if cfg.changed {
                let new = cfg.params.clone();
                cfg.changed = false;
                drop(cfg);

                if new.centre_freq() != current.centre_freq() {
                    let _ = sdr.set_center_freq(new.centre_freq());
                    eprintln!("[SDR] Freq  → {:.3} MHz", new.centre_freq_mhz);
                }
                if new.sample_rate() != current.sample_rate() {
                    let _ = sdr.set_sample_rate(new.sample_rate());
                    eprintln!("[SDR] Rate  → {:.3} MHz", new.sample_rate_mhz);
                }
                if new.gain_tenths() != current.gain_tenths() {
                    let _ = sdr.set_tuner_gain(TunerGain::Manual(new.gain_tenths()));
                    eprintln!("[SDR] Gain  → {:.1} dB", new.gain_db);
                }
                current = new;
            }
        }

        match sdr.read_sync(&mut buf_u8) {
            Ok(_)  => {}
            Err(e) => { eprintln!("[SDR] Read error: {:?}", e); break; }
        }

        if last_send.elapsed() < interval { continue; }
        last_send = Instant::now();

        let raw: Vec<i16> = buf_u8.iter().map(|&b| (b as i16) - 127).collect();
        let bins  = compute_spectrum(&raw, &fft, &window, window_power);
        let frame = encode_frame(&bins, &current);
        let _ = spectrum_tx.send(frame);
    }

    eprintln!("[SDR] Reader loop exited.");
}

// ── Control message handler ───────────────────────────────────────────────────

fn handle_control_message(
    text: &str,
    text_tx: &broadcast::Sender<String>,
    config: &Arc<Mutex<RunningConfig>>,
) {
    let cmd: serde_json::Value = match serde_json::from_str(text) {
        Ok(v)  => v,
        Err(e) => { eprintln!("[WS] Bad control JSON: {}", e); return; }
    };

    match cmd.get("cmd").and_then(|v| v.as_str()) {
        Some("apply") => {
            if let Some(p) = cmd.get("params") {
                if let Ok(new) = serde_json::from_value::<SdrParams>(p.clone()) {
                    {
                        let mut cfg = config.lock().unwrap();
                        cfg.params  = new.clone();
                        cfg.changed = true;
                    }
                    // Broadcast new config to all clients so displays update
                    let msg = serde_json::json!({"type":"config","params":new}).to_string();
                    let _ = text_tx.send(msg);
                    eprintln!("[CFG] Applied new params");
                }
            }
        }

        Some("save_preset") => {
            let name = cmd.get("name").and_then(|v| v.as_str()).unwrap_or("unnamed");
            if let Some(p) = cmd.get("params") {
                if let Ok(new) = serde_json::from_value::<SdrParams>(p.clone()) {
                    // Apply immediately
                    {
                        let mut cfg = config.lock().unwrap();
                        cfg.params  = new.clone();
                        cfg.changed = true;
                    }
                    // Save to disk
                    let mut profiles = load_profiles();
                    profiles.presets.insert(name.to_string(), new.clone());
                    profiles.last_used = name.to_string();
                    save_profiles(&profiles);
                    eprintln!("[CFG] Saved preset '{}'", name);

                    // Broadcast updated profiles + new config to all clients
                    let pmsg = serde_json::json!({
                        "type":      "profiles",
                        "presets":   profiles.presets,
                        "last_used": profiles.last_used,
                    }).to_string();
                    let cmsg = serde_json::json!({"type":"config","params":new}).to_string();
                    let _ = text_tx.send(pmsg);
                    let _ = text_tx.send(cmsg);
                }
            }
        }

        Some("delete_preset") => {
            let name = cmd.get("name").and_then(|v| v.as_str()).unwrap_or("");
            let mut profiles = load_profiles();
            if profiles.presets.len() > 1 {
                profiles.presets.remove(name);
                if profiles.last_used == name {
                    profiles.last_used = profiles.presets.keys().next()
                        .cloned().unwrap_or_default();
                }
                save_profiles(&profiles);
                eprintln!("[CFG] Deleted preset '{}'", name);
                let msg = serde_json::json!({
                    "type":      "profiles",
                    "presets":   profiles.presets,
                    "last_used": profiles.last_used,
                }).to_string();
                let _ = text_tx.send(msg);
            } else {
                eprintln!("[CFG] Cannot delete last preset");
            }
        }

        Some("shutdown") => {
            eprintln!("[CMD] Shutdown requested by client");
            let _ = std::process::Command::new("sudo")
                .args(["shutdown", "-h", "now"])
                .spawn();
        }

        other => eprintln!("[WS] Unknown cmd: {:?}", other),
    }
}

// ── HTTP server ───────────────────────────────────────────────────────────────

async fn run_http_server() {
    let addr = format!("0.0.0.0:{}", HTTP_PORT);
    let listener = TcpListener::bind(&addr).await
        .unwrap_or_else(|e| panic!("HTTP bind failed: {}", e));
    eprintln!("[HTTP] Listening on http://{}", addr);

    loop {
        match listener.accept().await {
            Ok((mut stream, peer)) => {
                eprintln!("[HTTP] Connection from {}", peer);
                tokio::spawn(async move {
                    let mut buf = [0u8; 2048];
                    let _ = stream.read(&mut buf).await;
                    let body   = HTML.as_bytes();
                    let header = format!(
                        "HTTP/1.1 200 OK\r\n\
                         Content-Type: text/html; charset=utf-8\r\n\
                         Content-Length: {}\r\n\
                         Connection: close\r\n\
                         \r\n",
                        body.len()
                    );
                    let _ = stream.write_all(header.as_bytes()).await;
                    let _ = stream.write_all(body).await;
                });
            }
            Err(e) => eprintln!("[HTTP] Accept error: {}", e),
        }
    }
}

// ── WebSocket handler ─────────────────────────────────────────────────────────

async fn handle_ws_client(
    stream: tokio::net::TcpStream,
    spectrum_tx: broadcast::Sender<Vec<u8>>,
    text_tx:     broadcast::Sender<String>,
    config:      Arc<Mutex<RunningConfig>>,
    peer:        std::net::SocketAddr,
) {
    let ws_stream = match accept_async(stream).await {
        Ok(ws) => ws,
        Err(e) => { eprintln!("[WS] Handshake failed: {:?}", e); return; }
    };
    eprintln!("[WS] Client connected: {}", peer);

    let (mut ws_tx, mut ws_rx) = ws_stream.split();
    let mut spectrum_rx = spectrum_tx.subscribe();
    let mut text_rx     = text_tx.subscribe();

    // Send initial state to this client immediately on connect
    {
        let profiles = load_profiles();
        let pmsg = serde_json::json!({
            "type":      "profiles",
            "presets":   profiles.presets,
            "last_used": profiles.last_used,
        }).to_string();
        let cmsg = serde_json::json!({
            "type":   "config",
            "params": config.lock().unwrap().params,
        }).to_string();
        let _ = ws_tx.send(Message::Text(pmsg.into())).await;
        let _ = ws_tx.send(Message::Text(cmsg.into())).await;
    }

    loop {
        tokio::select! {
            // Binary spectrum frames → client
            result = spectrum_rx.recv() => {
                match result {
                    Ok(frame) => {
                        if ws_tx.send(Message::Binary(frame.into())).await.is_err() { break; }
                    }
                    Err(broadcast::error::RecvError::Lagged(n)) => {
                        eprintln!("[WS] {} lagged spectrum, dropped {}", peer, n);
                    }
                    Err(_) => break,
                }
            }
            // Text config/profile updates → client
            result = text_rx.recv() => {
                match result {
                    Ok(text) => {
                        if ws_tx.send(Message::Text(text.into())).await.is_err() { break; }
                    }
                    Err(broadcast::error::RecvError::Lagged(n)) => {
                        eprintln!("[WS] {} lagged text, dropped {}", peer, n);
                    }
                    Err(_) => break,
                }
            }
            // Incoming control messages from client
            msg = ws_rx.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        handle_control_message(&text, &text_tx, &config);
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Err(e)) => {
                        eprintln!("[WS] Error from {}: {:?}", peer, e);
                        break;
                    }
                    _ => {}
                }
            }
        }
    }

    eprintln!("[WS] Client disconnected: {}", peer);
}

// ── WebSocket server ──────────────────────────────────────────────────────────

async fn run_ws_server(
    spectrum_tx: broadcast::Sender<Vec<u8>>,
    text_tx:     broadcast::Sender<String>,
    config:      Arc<Mutex<RunningConfig>>,
) {
    let addr = format!("0.0.0.0:{}", WS_PORT);
    let listener = TcpListener::bind(&addr).await
        .unwrap_or_else(|e| panic!("WS bind failed: {}", e));
    eprintln!("[WS]   Listening on ws://{}", addr);

    loop {
        match listener.accept().await {
            Ok((stream, peer)) => {
                let stx = spectrum_tx.clone();
                let ttx = text_tx.clone();
                let cfg = config.clone();
                tokio::spawn(handle_ws_client(stream, stx, ttx, cfg, peer));
            }
            Err(e) => eprintln!("[WS] Accept error: {}", e),
        }
    }
}

// ── Entry point ───────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() {
    // Load profiles and pick the last-used preset as starting config
    let profiles = load_profiles();
    let initial  = profiles.presets.get(&profiles.last_used)
        .cloned()
        .unwrap_or_default();

    eprintln!("╔══════════════════════════════════════════════╗");
    eprintln!("║  SDR WebSocket Server                        ║");
    eprintln!("╠══════════════════════════════════════════════╣");
    eprintln!("║  Waterfall: http://10.42.0.1:{}           ║", HTTP_PORT);
    eprintln!("║  WebSocket: ws://10.42.0.1:{}             ║", WS_PORT);
    eprintln!("║  Preset:    {}", profiles.last_used);
    eprintln!("╚══════════════════════════════════════════════╝");

    let config = Arc::new(Mutex::new(RunningConfig {
        params:  initial,
        changed: false,
    }));

    let (spectrum_tx, _) = broadcast::channel::<Vec<u8>>(BROADCAST_CAP);
    let (text_tx,     _) = broadcast::channel::<String>(BROADCAST_CAP);

    // SDR reader in a dedicated OS thread (blocking USB I/O)
    let stx2 = spectrum_tx.clone();
    let cfg2 = config.clone();
    std::thread::spawn(move || sdr_reader(stx2, cfg2));

    tokio::join!(
        run_http_server(),
        run_ws_server(spectrum_tx, text_tx, config),
    );
}
