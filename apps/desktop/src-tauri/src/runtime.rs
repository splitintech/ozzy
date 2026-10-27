use base64::{engine::general_purpose::STANDARD, Engine};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};
use tauri::{AppHandle, Emitter, Manager};

#[derive(Deserialize)]
struct Envelope {
    payload: String,
    signature: String,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Manifest {
    pub schema: u32,
    pub version: String,
    pub sequence: u64,
    pub platform: String,
    pub url: String,
    pub bytes: u64,
    pub unpacked_bytes: u64,
    pub sha256: String,
    pub executable: String,
}
pub fn platform() -> &'static str {
    if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        "aarch64-apple-darwin"
    } else if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        "x86_64-pc-windows-msvc"
    } else {
        "unsupported"
    }
}
pub fn root(app: &AppHandle) -> Result<PathBuf, String> {
    let path = app
        .path()
        .app_data_dir()
        .map_err(|_| "Cannot locate application data")?;
    fs::create_dir_all(&path).map_err(|_| "Cannot create application data")?;
    Ok(path)
}
fn http() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .https_only(true)
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(1800))
        .build()
        .map_err(|_| "Cannot initialize downloads".into())
}
pub fn validate(m: &Manifest) -> Result<(), String> {
    if m.schema != 1
        || m.platform != platform()
        || m.bytes == 0
        || m.bytes > 8_000_000_000
        || m.unpacked_bytes < m.bytes
        || m.unpacked_bytes > 16_000_000_000
        || m.sha256.len() != 64
        || !m.sha256.bytes().all(|b| b.is_ascii_hexdigit())
        || m.sequence == 0
        || !m
            .version
            .bytes()
            .next()
            .is_some_and(|b| b.is_ascii_alphanumeric())
        || !m
            .version
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".-".contains(&b))
        || m.executable
            != if cfg!(windows) {
                "voice/voice.exe"
            } else {
                "voice/voice"
            }
    {
        return Err("Incompatible or invalid runtime manifest".into());
    }
    let url = url::Url::parse(&m.url).map_err(|_| "Invalid pack URL")?;
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || url.host_str().is_none()
    {
        return Err("Runtime downloads require HTTPS without credentials in the URL".into());
    }
    Ok(())
}
pub async fn manifest(url: &str) -> Result<Manifest, String> {
    let key_text = option_env!("VOICE_RUNTIME_PUBLIC_KEY").unwrap_or("");
    if key_text.is_empty() {
        return Err("This preview has no release verification key. Use a signed release or the source-development runtime.".into());
    }
    let response = http()?
        .get(url)
        .send()
        .await
        .map_err(|_| "Could not fetch runtime manifest")?
        .error_for_status()
        .map_err(|_| "Manifest download failed")?;
    let mut stream = response.bytes_stream();
    let mut bytes = Vec::new();
    while let Some(chunk) = stream.next().await {
        bytes.extend_from_slice(&chunk.map_err(|_| "Manifest download interrupted")?);
        if bytes.len() > 64_000 {
            return Err("Manifest is too large".into());
        }
    }
    decode_manifest(&bytes, key_text)
}

fn decode_manifest(bytes: &[u8], key_text: &str) -> Result<Manifest, String> {
    let key_bytes: [u8; 32] = STANDARD
        .decode(key_text)
        .map_err(|_| "Invalid verification key")?
        .try_into()
        .map_err(|_| "Invalid verification key")?;
    let key = VerifyingKey::from_bytes(&key_bytes).map_err(|_| "Invalid verification key")?;
    let envelope: Envelope =
        serde_json::from_slice(&bytes).map_err(|_| "Malformed runtime manifest")?;
    let payload = STANDARD
        .decode(envelope.payload)
        .map_err(|_| "Malformed payload")?;
    let signature = Signature::from_slice(
        &STANDARD
            .decode(envelope.signature)
            .map_err(|_| "Malformed signature")?,
    )
    .map_err(|_| "Malformed signature")?;
    key.verify(&payload, &signature)
        .map_err(|_| "Runtime signature verification failed")?;
    let m: Manifest = serde_json::from_slice(&payload).map_err(|_| "Malformed runtime metadata")?;
    validate(&m)?;
    Ok(m)
}
pub fn installed(app: &AppHandle) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    if let Ok(path) = std::env::var("VOICE_DEV_EXECUTABLE") {
        let path = PathBuf::from(path);
        if path.is_absolute() && path.is_file() {
            return Ok(path);
        }
    }
    let root = root(app)?;
    let m: Manifest = serde_json::from_slice(
        &fs::read(root.join("runtime.json")).map_err(|_| "Install the local runtime first")?,
    )
    .map_err(|_| "Invalid installed runtime metadata")?;
    validate(&m)?;
    let executable = root.join("runtimes").join(&m.version).join(&m.executable);
    if !executable.is_file() {
        return Err("Runtime is incomplete. Install it again.".into());
    }
    Ok(executable)
}
fn unpack(pack: &Path, directory: &Path, limit: u64) -> Result<(), String> {
    let mut archive = zip::ZipArchive::new(fs::File::open(pack).map_err(|_| "Cannot read pack")?)
        .map_err(|_| "Invalid archive")?;
    if archive.len() > 100_000 {
        return Err("Too many runtime files".into());
    }
    let mut total = 0u64;
    let mut names = std::collections::HashSet::new();
    for i in 0..archive.len() {
        let mut item = archive.by_index(i).map_err(|_| "Corrupt archive")?;
        let relative = item.enclosed_name().ok_or("Unsafe archive path")?;
        if item.name().contains('\\')
            || item.name().contains(':')
            || !names.insert(relative.to_string_lossy().to_lowercase())
        {
            return Err("Unsafe or duplicate archive path".into());
        }
        let mode = item.unix_mode().unwrap_or(0);
        if mode & 0o170000 == 0o120000 {
            return Err("Archive symbolic links are forbidden".into());
        }
        total = total.checked_add(item.size()).ok_or("Archive too large")?;
        if total > limit {
            return Err("Archive exceeds declared size".into());
        }
        let target = directory.join(relative);
        if item.is_dir() {
            fs::create_dir_all(&target).map_err(|_| "Cannot create directory")?;
            continue;
        }
        fs::create_dir_all(target.parent().ok_or("Invalid path")?)
            .map_err(|_| "Cannot create directory")?;
        let mut file = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&target)
            .map_err(|_| "Cannot create file")?;
        let copied = std::io::copy(&mut item, &mut file).map_err(|_| "Extraction failed")?;
        if copied != item.size() {
            return Err("Truncated runtime file".into());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                &target,
                fs::Permissions::from_mode(if mode & 0o111 != 0 { 0o700 } else { 0o600 }),
            )
            .map_err(|_| "Cannot protect runtime file")?;
        }
    }
    Ok(())
}
pub async fn install(app: AppHandle, url: String) -> Result<Manifest, String> {
    let m = manifest(&url).await?;
    let root = root(&app)?;
    let lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(root.join("runtime-install.lock"))
        .map_err(|_| "Cannot lock runtime installation")?;
    fs2::FileExt::try_lock_exclusive(&lock)
        .map_err(|_| "Another application instance is installing a runtime")?;
    check_sequence(&root, &m)?;
    let need = m.bytes + m.unpacked_bytes + 512_000_000;
    if fs2::available_space(&root).map_err(|_| "Cannot inspect disk space")? < need {
        return Err(format!(
            "Free at least {:.1} GB for installation.",
            need as f64 / 1e9
        ));
    }
    let temp = tempfile::Builder::new()
        .prefix("runtime-install-")
        .tempdir_in(&root)
        .map_err(|_| "Cannot prepare installation")?;
    let pack = temp.path().join("runtime.zip");
    let mut file = tokio::fs::File::create(&pack)
        .await
        .map_err(|_| "Cannot create download")?;
    let response = http()?
        .get(&m.url)
        .send()
        .await
        .map_err(|_| "Download could not start")?
        .error_for_status()
        .map_err(|_| "Download failed")?;
    let mut stream = response.bytes_stream();
    let mut size = 0u64;
    let mut hash = Sha256::new();
    let mut reported = 0u64;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| "Download interrupted. Retry safely.")?;
        size += chunk.len() as u64;
        if size > m.bytes {
            return Err("Download exceeded signed size".into());
        }
        hash.update(&chunk);
        tokio::io::AsyncWriteExt::write_all(&mut file, &chunk)
            .await
            .map_err(|_| "Cannot write download")?;
        if size - reported > 4_000_000 {
            let _ = app.emit(
                "runtime-download",
                serde_json::json!({"downloaded":size,"total":m.bytes}),
            );
            reported = size;
        }
    }
    file.sync_all()
        .await
        .map_err(|_| "Cannot finish download")?;
    drop(file);
    if size != m.bytes || hex::encode(hash.finalize()) != m.sha256.to_lowercase() {
        return Err("Runtime checksum verification failed".into());
    }
    let stage = temp.path().join("extracted");
    let stage2 = stage.clone();
    let limit = m.unpacked_bytes;
    tokio::task::spawn_blocking(move || unpack(&pack, &stage2, limit))
        .await
        .map_err(|_| "Extraction stopped")??;
    if !stage.join(&m.executable).is_file() {
        return Err("Runtime executable is missing".into());
    }
    // Readiness must succeed before changing either the runtime or its pointer.
    // Use private scratch data, never the owner's database or model directory.
    let output = tokio::time::timeout(
        std::time::Duration::from_secs(240),
        tokio::process::Command::new(stage.join(&m.executable))
            .arg("--runtime-self-test")
            .current_dir(temp.path())
            .env("VOICE_MODE", "local")
            .env("VOICE_DATABASE_URL", "")
            .env("VOICE_DATA_DIR", temp.path().join("probe-data"))
            .env("NUMBA_CACHE_DIR", temp.path().join("probe-cache"))
            .env("HF_HUB_OFFLINE", "1")
            .env("TRANSFORMERS_OFFLINE", "1")
            .stdin(std::process::Stdio::null())
            .kill_on_drop(true)
            .output(),
    )
    .await
    .map_err(|_| "Runtime readiness check timed out; previous runtime preserved")?
    .map_err(|_| "Runtime cannot run on this machine; previous runtime preserved")?;
    if !output.status.success() {
        return Err("Runtime readiness failed; previous runtime preserved".into());
    }
    activate(&root, &stage, temp.path(), &m)?;
    Ok(m)
}

fn check_sequence(root: &Path, m: &Manifest) -> Result<(), String> {
    for name in ["runtime.json", "runtime.highest.json"] {
        let path = root.join(name);
        if path.exists() {
            let previous: Manifest =
                serde_json::from_slice(&fs::read(path).map_err(|_| "Cannot read runtime history")?)
                    .map_err(|_| "Invalid runtime history")?;
            validate(&previous)?;
            if m.sequence < previous.sequence {
                return Err("An older runtime cannot replace the current release".into());
            }
            if m.sequence == previous.sequence
                && (m.sha256 != previous.sha256 || m.version != previous.version)
            {
                return Err("A runtime sequence cannot be reused for different contents".into());
            }
        }
    }
    Ok(())
}

fn write_manifest(path: &Path, m: &Manifest) -> Result<(), String> {
    let mut next = tempfile::NamedTempFile::new_in(path.parent().ok_or("Invalid metadata path")?)
        .map_err(|_| "Cannot create activation metadata")?;
    next.write_all(&serde_json::to_vec(m).map_err(|_| "Invalid metadata")?)
        .map_err(|_| "Cannot write metadata")?;
    next.as_file()
        .sync_all()
        .map_err(|_| "Cannot persist metadata")?;
    next.persist(path).map_err(|_| "Cannot activate runtime")?;
    Ok(())
}

fn activate(root: &Path, stage: &Path, temporary: &Path, m: &Manifest) -> Result<(), String> {
    let current = root.join("runtime.json");
    let runtimes = root.join("runtimes");
    fs::create_dir_all(&runtimes).map_err(|_| "Cannot create runtime storage")?;
    let target = runtimes.join(&m.version);
    if target.exists() {
        if let Ok(bytes) = fs::read(&current) {
            if let Ok(previous) = serde_json::from_slice::<Manifest>(&bytes) {
                if previous.version == m.version {
                    if previous.sha256 != m.sha256 {
                        return Err(
                            "A runtime version cannot be replaced by different contents".into()
                        );
                    }
                }
            }
        }
        // Recover an interrupted activation using freshly verified extracted files.
        fs::rename(&target, temporary.join("abandoned-runtime"))
            .map_err(|_| "Close other instances before retrying runtime installation")?;
    }
    if fs::rename(stage, &target).is_err() {
        let abandoned = temporary.join("abandoned-runtime");
        if abandoned.exists() {
            let _ = fs::rename(abandoned, &target);
        }
        return Err("Cannot activate runtime directory".into());
    }
    if current.exists() {
        let previous: Manifest =
            serde_json::from_slice(&fs::read(&current).map_err(|_| "Cannot read current runtime")?)
                .map_err(|_| "Invalid current runtime")?;
        if previous.version != m.version {
            write_manifest(&root.join("runtime.previous.json"), &previous)?;
        }
    }
    write_manifest(&root.join("runtime.highest.json"), m)?;
    write_manifest(&current, m)
}
#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(version: &str, sequence: u64) -> Manifest {
        Manifest {
            schema: 1,
            version: version.into(),
            sequence,
            platform: platform().into(),
            url: "https://example.test/runtime.zip".into(),
            bytes: 100,
            unpacked_bytes: 200,
            sha256: format!("{:064x}", sequence),
            executable: if cfg!(windows) {
                "voice/voice.exe"
            } else {
                "voice/voice"
            }
            .into(),
        }
    }
    fn staged(root: &Path, m: &Manifest) -> PathBuf {
        let path = root.join(format!("stage-{}", m.version));
        let executable = path.join(&m.executable);
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(executable, b"verified fixture").unwrap();
        path
    }
    #[test]
    fn activation_keeps_previous_runtime_and_prevents_replay_after_rollback() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let first = fixture("0.1.0", 1);
        let second = fixture("0.2.0", 2);
        activate(root, &staged(root, &first), root, &first).unwrap();
        activate(root, &staged(root, &second), root, &second).unwrap();
        let previous: Manifest =
            serde_json::from_slice(&fs::read(root.join("runtime.previous.json")).unwrap()).unwrap();
        assert_eq!(previous.version, first.version);
        assert!(root
            .join("runtimes/0.1.0")
            .join(&first.executable)
            .is_file());
        // Explicit rollback changes the current pointer, not the release high-water mark.
        write_manifest(&root.join("runtime.json"), &previous).unwrap();
        assert!(check_sequence(root, &first).is_err());
        assert!(check_sequence(root, &second).is_ok());
        let mut reused = second.clone();
        reused.sha256 = "f".repeat(64);
        assert!(check_sequence(root, &reused).is_err());
    }
    #[test]
    fn failed_activation_preserves_current_and_can_be_retried() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let first = fixture("0.1.0", 1);
        let second = fixture("0.2.0", 2);
        activate(root, &staged(root, &first), root, &first).unwrap();
        let bytes = fs::read(root.join("runtime.json")).unwrap();
        assert!(activate(root, &root.join("missing"), root, &second).is_err());
        assert_eq!(fs::read(root.join("runtime.json")).unwrap(), bytes);
        activate(root, &staged(root, &second), root, &second).unwrap();
        assert!(check_sequence(root, &second).is_ok());
    }
    #[test]
    fn identical_pack_repairs_files_without_destroying_rollback() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let first = fixture("0.1.0", 1);
        let second = fixture("0.2.0", 2);
        activate(root, &staged(root, &first), root, &first).unwrap();
        activate(root, &staged(root, &second), root, &second).unwrap();
        let executable = root.join("runtimes/0.2.0").join(&second.executable);
        fs::write(&executable, b"damaged").unwrap();
        activate(root, &staged(root, &second), root, &second).unwrap();
        assert_eq!(fs::read(executable).unwrap(), b"verified fixture");
        let previous: Manifest =
            serde_json::from_slice(&fs::read(root.join("runtime.previous.json")).unwrap()).unwrap();
        assert_eq!(previous.version, first.version);
    }
    fn archive(name: &str, limit: u64) -> Result<(), String> {
        let dir = tempfile::tempdir().unwrap();
        let pack = dir.path().join("bad.zip");
        let mut zip = zip::ZipWriter::new(fs::File::create(&pack).unwrap());
        zip.start_file(name, zip::write::SimpleFileOptions::default())
            .unwrap();
        zip.write_all(b"oversized").unwrap();
        zip.finish().unwrap();
        unpack(&pack, &dir.path().join("out"), limit)
    }
    #[test]
    fn signed_manifest_rejects_tampering_and_unsafe_versions() {
        use ed25519_dalek::{Signer, SigningKey};
        let key = SigningKey::from_bytes(&[5; 32]);
        let public = STANDARD.encode(key.verifying_key().as_bytes());
        let payload = serde_json::to_vec(&Manifest {
            schema: 1,
            version: "0.1.0".into(),
            sequence: 1,
            platform: platform().into(),
            url: "https://example.test/runtime.zip".into(),
            bytes: 100,
            unpacked_bytes: 200,
            sha256: "a".repeat(64),
            executable: if cfg!(windows) {
                "voice/voice.exe"
            } else {
                "voice/voice"
            }
            .into(),
        })
        .unwrap();
        let signature = STANDARD.encode(key.sign(&payload).to_bytes());
        let envelope =
            serde_json::json!({"payload": STANDARD.encode(&payload), "signature": signature});
        assert!(decode_manifest(&serde_json::to_vec(&envelope).unwrap(), &public).is_ok());
        let mut damaged = envelope.clone();
        damaged["signature"] = serde_json::Value::String(STANDARD.encode([0u8; 64]));
        assert!(decode_manifest(&serde_json::to_vec(&damaged).unwrap(), &public).is_err());
        let mut metadata: Manifest = serde_json::from_slice(&payload).unwrap();
        metadata.version = "..".into();
        assert!(validate(&metadata).is_err());
        metadata.version = "0.1.0".into();
        metadata.sequence = 0;
        assert!(validate(&metadata).is_err());
    }
    #[test]
    fn refuses_traversal() {
        assert!(archive("../escape", 100).is_err());
    }
    #[test]
    fn limits_expansion() {
        assert!(archive("voice/voice", 2).is_err());
    }
}
