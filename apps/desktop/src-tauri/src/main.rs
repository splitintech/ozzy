#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod runtime;
use rand::RngCore;
use serde::Serialize;
use std::{
    fs,
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_updater::UpdaterExt;
#[derive(Clone)]
struct Connection {
    base: url::Url,
    token: String,
    client: reqwest::Client,
    mode: String,
}
#[derive(Default)]
struct NativeState {
    connection: Mutex<Option<Connection>>,
    child: Mutex<Option<Child>>,
    install: tokio::sync::Mutex<()>,
}
#[derive(Serialize)]
struct ApiResponse {
    status: u16,
    body: String,
}
fn connection(state: &NativeState) -> Result<Connection, String> {
    state
        .connection
        .lock()
        .map_err(|_| "Connection lock unavailable")?
        .clone()
        .ok_or("Open a workspace first".into())
}
fn api_url(base: &url::Url, path: &str) -> Result<url::Url, String> {
    if !path.starts_with("/v1/")
        || path.contains("..")
        || path.contains(['%', '?', '#', '\\'])
        || path.len() > 512
    {
        return Err("Invalid API path".into());
    }
    let mut url = base.clone();
    url.set_path(path);
    Ok(url)
}
fn api_builder(
    c: &Connection,
    path: &str,
    method: reqwest::Method,
) -> Result<reqwest::RequestBuilder, String> {
    let mut request = c
        .client
        .request(method, api_url(&c.base, path)?)
        .header("Origin", c.base.origin().ascii_serialization());
    if !c.token.is_empty() {
        request = request.bearer_auth(&c.token);
    }
    Ok(request)
}
async fn response(response: reqwest::Response) -> Result<ApiResponse, String> {
    let status = response.status().as_u16();
    let body = response
        .text()
        .await
        .map_err(|_| "Could not read API response")?;
    Ok(ApiResponse { status, body })
}
fn new_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .cookie_store(true)
        .timeout(Duration::from_secs(90))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Cannot initialize connection".into())
}
fn terminate(child: &mut Child) {
    #[cfg(unix)]
    unsafe {
        libc::kill(child.id() as i32, libc::SIGTERM);
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/T", "/F", "/PID", &child.id().to_string()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    for _ in 0..30 {
        if child.try_wait().ok().flatten().is_some() {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let _ = child.kill();
    let _ = child.wait();
}
#[tauri::command]
async fn runtime_status(app: AppHandle) -> Result<serde_json::Value, String> {
    let executable = match runtime::installed(&app) {
        Ok(path) => path,
        Err(_) => {
            return Ok(
                serde_json::json!({"platform": runtime::platform(), "installed": false, "available_devices": ["cpu"]}),
            )
        }
    };
    let root = runtime::root(&app)?;
    let output = tokio::time::timeout(
        Duration::from_secs(45),
        tokio::process::Command::new(executable)
            .args(["doctor", "--check-acceleration"])
            .env("VOICE_MODE", "local")
            .env("VOICE_DATABASE_URL", "")
            .env("VOICE_DATA_DIR", root.join("local"))
            .env("VOICE_MODEL_DIR", root.join("models"))
            .stdin(Stdio::null())
            .kill_on_drop(true)
            .output(),
    )
    .await
    .map_err(|_| "Runtime inspection timed out")?
    .map_err(|_| "Cannot inspect the runtime")?;
    if !output.status.success() {
        return Err("Installed runtime failed its readiness inspection".into());
    }
    let mut info: serde_json::Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| "Invalid runtime inspection response")?;
    info["installed"] = serde_json::Value::Bool(true);
    info["platform"] = serde_json::Value::String(runtime::platform().into());
    Ok(info)
}

#[tauri::command]
async fn start_local(
    app: AppHandle,
    state: State<'_, NativeState>,
    device: Option<String>,
) -> Result<(), String> {
    let device = device.unwrap_or_else(|| "cpu".into());
    if !matches!(device.as_str(), "cpu" | "mps" | "cuda") {
        return Err("Unsupported inference device".into());
    }
    let executable = runtime::installed(&app)?;
    if let Some(mut old) = state
        .child
        .lock()
        .map_err(|_| "Process lock unavailable")?
        .take()
    {
        terminate(&mut old);
    }
    let listener =
        std::net::TcpListener::bind("127.0.0.1:0").map_err(|_| "No loopback port available")?;
    let port = listener
        .local_addr()
        .map_err(|_| "Cannot allocate port")?
        .port();
    drop(listener);
    let mut random = [0u8; 32];
    rand::rng().fill_bytes(&mut random);
    let token = hex::encode(random);
    let data = runtime::root(&app)?;
    let mut cmd = Command::new(&executable);
    cmd.args(["local", "--port", &port.to_string(), "--device", &device])
        .env("VOICE_MODE", "local")
        .env("VOICE_ROLE", "controller")
        .env("VOICE_LOCAL_TOKEN", &token)
        .env("VOICE_DATA_DIR", data.join("local"))
        .env("VOICE_MODEL_DIR", data.join("models"))
        .env(
            "VOICE_FFMPEG_DIR",
            executable
                .parent()
                .ok_or("Invalid runtime path")?
                .join("_internal")
                .join("ffmpeg"),
        )
        .env("VOICE_DATABASE_URL", "")
        .env("VOICE_STATIC_DIR", data.join("unused-static"))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }
    *state.child.lock().map_err(|_| "Process lock unavailable")? =
        Some(cmd.spawn().map_err(|_| "Local engine could not start")?);
    let client = new_client()?;
    let base =
        url::Url::parse(&format!("http://127.0.0.1:{port}")).map_err(|_| "Invalid local URL")?;
    for _ in 0..120 {
        if let Ok(response) = client.get(base.join("health").unwrap()).send().await {
            if response.status().is_success() {
                *state
                    .connection
                    .lock()
                    .map_err(|_| "Connection lock unavailable")? = Some(Connection {
                    base,
                    token,
                    client,
                    mode: "local".into(),
                });
                return Ok(());
            }
        }
        {
            let mut guard = state.child.lock().map_err(|_| "Process lock unavailable")?;
            if guard
                .as_mut()
                .and_then(|c| c.try_wait().ok().flatten())
                .is_some()
            {
                return Err(
                    "Local service exited during startup. Check runtime compatibility.".into(),
                );
            }
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    if let Some(mut child) = state
        .child
        .lock()
        .map_err(|_| "Process lock unavailable")?
        .take()
    {
        terminate(&mut child);
    }
    Err("Local service did not become ready within sixty seconds".into())
}
#[tauri::command]
async fn api_request(
    state: State<'_, NativeState>,
    path: String,
    method: String,
    body: Option<String>,
    idempotency_key: Option<String>,
) -> Result<ApiResponse, String> {
    let c = connection(&state)?;
    let method = match method.as_str() {
        "GET" => reqwest::Method::GET,
        "POST" => reqwest::Method::POST,
        "DELETE" => reqwest::Method::DELETE,
        _ => return Err("Unsupported method".into()),
    };
    if body.as_ref().is_some_and(|b| b.len() > 32_000) {
        return Err("Request too large".into());
    }
    let mut request = api_builder(&c, &path, method)?;
    if let Some(body) = body {
        request = request
            .header("Content-Type", "application/json")
            .body(body);
    }
    if let Some(key) = idempotency_key {
        request = request.header("Idempotency-Key", key);
    }
    response(
        request
            .send()
            .await
            .map_err(|_| "Workspace unavailable. Reconnect if its session expired.")?,
    )
    .await
}
#[tauri::command]
async fn import_voice(
    state: State<'_, NativeState>,
    bytes: Vec<u8>,
    name: String,
    start: f64,
    end: f64,
    authorized: bool,
    idempotency_key: Option<String>,
) -> Result<ApiResponse, String> {
    if bytes.len() > 50 * 1024 * 1024 {
        return Err("Recordings must be under 50 MB".into());
    }
    let c = connection(&state)?;
    let form = reqwest::multipart::Form::new()
        .part(
            "file",
            reqwest::multipart::Part::bytes(bytes).file_name("reference.audio"),
        )
        .text("name", name)
        .text("start", start.to_string())
        .text("end", end.to_string())
        .text("authorized", authorized.to_string());
    let mut request = api_builder(&c, "/v1/voices", reqwest::Method::POST)?;
    if let Some(key) = idempotency_key {
        if key.len() > 128 || !key.is_ascii() { return Err("Invalid operation key".into()); }
        request = request.header("Idempotency-Key", key);
    }
    response(
        request.multipart(form)
            .send()
            .await
            .map_err(|_| "Reference import failed")?,
    )
    .await
}
#[derive(Serialize)]
struct Audio {
    bytes: Vec<u8>,
    content_type: String,
}
async fn audio(c: &Connection, path: &str) -> Result<Audio, String> {
    if !(path.starts_with("/v1/artifacts/")
        || (path.starts_with("/v1/voices/") && path.ends_with("/export")))
    {
        return Err("Invalid artifact path".into());
    }
    let response = api_builder(c, path, reqwest::Method::GET)?
        .send()
        .await
        .map_err(|_| "Could not download audio")?
        .error_for_status()
        .map_err(|_| "Audio unavailable or expired")?;
    let content_type = response
        .headers()
        .get("Content-Type")
        .and_then(|h| h.to_str().ok())
        .unwrap_or("application/octet-stream")
        .to_string();
    use futures_util::StreamExt;
    let mut bytes = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        bytes.extend_from_slice(&chunk.map_err(|_| "Download interrupted")?);
        if bytes.len() > 80_000_000 {
            return Err("Artifact too large".into());
        }
    }
    Ok(Audio {
        bytes,
        content_type,
    })
}
#[tauri::command]
async fn artifact_bytes(state: State<'_, NativeState>, path: String) -> Result<Audio, String> {
    audio(&connection(&state)?, &path).await
}
#[tauri::command]
async fn save_artifact(
    app: AppHandle,
    state: State<'_, NativeState>,
    path: String,
    filename: String,
) -> Result<(), String> {
    let data = audio(&connection(&state)?, &path).await?;
    let safe = std::path::Path::new(&filename)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("voice.wav")
        .to_string();
    let selected = tauri::async_runtime::spawn_blocking(move || {
        app.dialog().file().set_file_name(safe).blocking_save_file()
    })
    .await
    .map_err(|_| "Save dialog unavailable")?;
    if let Some(selected) = selected {
        tokio::fs::write(
            selected.into_path().map_err(|_| "Invalid destination")?,
            data.bytes,
        )
        .await
        .map_err(|_| "Could not save audio")?;
    }
    Ok(())
}
#[tauri::command]
async fn inspect_runtime(manifest_url: String) -> Result<runtime::Manifest, String> {
    runtime::manifest(&manifest_url).await
}
#[tauri::command]
async fn install_runtime(
    app: AppHandle,
    state: State<'_, NativeState>,
    manifest_url: String,
) -> Result<runtime::Manifest, String> {
    let _guard = state
        .install
        .try_lock()
        .map_err(|_| "Runtime installation already in progress")?;
    runtime::install(app, manifest_url).await
}
#[tauri::command]
async fn hosted_deployments(app: AppHandle) -> Result<serde_json::Value, String> {
    let output = tokio::process::Command::new(runtime::installed(&app)?)
        .args(["cloud", "deployments"]).stdin(Stdio::null()).output().await
        .map_err(|_| "Cannot list deployments")?;
    serde_json::from_slice(&output.stdout).map_err(|_| "Cannot read deployment metadata".into())
}

#[tauri::command]
async fn hosted_action(app: AppHandle, state: State<'_, NativeState>, payload: serde_json::Value) -> Result<serde_json::Value, String> {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    let _guard = state.install.try_lock().map_err(|_| "Another runtime operation is in progress")?;
    let action = payload["action"].as_str().ok_or("Missing action")?;
    if !["setup", "start", "status", "stop", "backup"].contains(&action) { return Err("Unsupported hosted action".into()); }
    let deployment = payload["deployment"].as_str().ok_or("Missing deployment")?;
    if deployment.is_empty() || deployment.len() > 48 || !deployment.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-') {
        return Err("Invalid deployment name".into());
    }
    let mut command = tokio::process::Command::new(runtime::installed(&app)?);
    command.args(["cloud", action, "--deployment", deployment]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).kill_on_drop(true);
    if action == "setup" {
        command.args(["--credentials-stdin", "--no-open", "--json"]);
        if let Some(source) = payload["source"].as_str() { command.args(["--source", source]); }
        if let Some(region) = payload["region"].as_str() { command.args(["--region", region]); }
    }
    let mut child = command.spawn().map_err(|_| "Cannot start hosted launcher")?;
    if let Some(mut stdin) = child.stdin.take() {
        if action == "setup" { stdin.write_all(payload.to_string().as_bytes()).await.map_err(|_| "Cannot deliver protected setup input")?; }
        stdin.shutdown().await.map_err(|_| "Cannot finish protected setup input")?;
    }
    let stderr = child.stderr.take().ok_or("Missing progress stream")?;
    let events = app.clone();
    let progress = tokio::spawn(async move {
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Ok(value) = serde_json::from_str::<serde_json::Value>(&line) {
                if let Some(phase) = value["phase"].as_str() {
                    if ["preflight", "building", "volumes", "starting", "bootstrapping", "checking", "ready"].contains(&phase) {
                        let _ = events.emit("hosted-progress", phase);
                    }
                }
            }
        }
    });
    let output = tokio::time::timeout(Duration::from_secs(2400), child.wait_with_output()).await
        .map_err(|_| "Setup timed out; retry to reconcile existing resources")?
        .map_err(|_| "Hosted launcher failed")?;
    let _ = progress.await;
    let result: serde_json::Value = serde_json::from_slice(&output.stdout).map_err(|_| "Invalid launcher result")?;
    if !output.status.success() { return Err(result["message"].as_str().unwrap_or("Hosted operation failed").into()); }
    if let Some(destination) = result["url"].as_str() {
        let base = url::Url::parse(destination).map_err(|_| "Invalid hosted URL")?;
        if base.scheme() != "https" { return Err("Hosted access requires HTTPS".into()); }
        *state.connection.lock().map_err(|_| "Connection lock unavailable")? = Some(Connection { base, token: String::new(), client: new_client()?, mode: "hosted".into() });
    }
    Ok(result)
}

async fn cloud_command(
    app: &AppHandle,
    args: Vec<String>,
    key: &str,
) -> Result<serde_json::Value, String> {
    let mut command = tokio::process::Command::new(runtime::installed(app)?);
    command
        .args(args)
        .env("VOICE_DAYTONA_API_KEY", key)
        .env("VOICE_MODE", "local")
        .stdin(Stdio::null())
        .kill_on_drop(true);
    let output = tokio::time::timeout(Duration::from_secs(240), command.output())
        .await
        .map_err(|_| "Daytona timed out. Check status before retrying.")?
        .map_err(|_| "Launcher could not start")?;
    if !output.status.success() {
        return Err("Daytona request failed. Check credentials, snapshot, secrets, and quota using the CLI.".into());
    }
    serde_json::from_slice(&output.stdout).map_err(|_| "Invalid launcher response".into())
}
#[tauri::command]
async fn cloud_start(
    app: AppHandle,
    state: State<'_, NativeState>,
    api_key: Option<String>,
    snapshot: String,
    secret_names: serde_json::Value,
) -> Result<(), String> {
    let entry = keyring::Entry::new("studio.voicestudio.app", "daytona-api-key")
        .map_err(|_| "OS credential storage unavailable")?;
    let key = if let Some(value) = api_key.filter(|s| !s.is_empty()) {
        entry
            .set_password(&value)
            .map_err(|_| "Cannot protect credential")?;
        value
    } else {
        entry
            .get_password()
            .map_err(|_| "Enter a Daytona API key")?
    };
    let result = cloud_command(
        &app,
        vec![
            "cloud".into(),
            "start".into(),
            "--json".into(),
            "--snapshot".into(),
            snapshot,
            "--secret-names".into(),
            secret_names.to_string(),
        ],
        &key,
    )
    .await?;
    let base = url::Url::parse(result["url"].as_str().ok_or("Missing signed preview URL")?)
        .map_err(|_| "Invalid hosted URL")?;
    if base.scheme() != "https" {
        return Err("Hosted access requires HTTPS".into());
    }
    let client = new_client()?;
    let mut ready = false;
    for _ in 0..60 {
        let mut health = base.clone();
        health.set_path("/health");
        if let Ok(response) = client.get(health).send().await {
            if response.status().is_success() {
                ready = true;
                break;
            }
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
    if !ready {
        return Err("Hosted API did not become ready. Inspect cloud status.".into());
    }
    *state
        .connection
        .lock()
        .map_err(|_| "Connection lock unavailable")? = Some(Connection {
        base,
        token: String::new(),
        client,
        mode: "hosted".into(),
    });
    Ok(())
}
#[tauri::command]
async fn cloud_status(app: AppHandle) -> Result<serde_json::Value, String> {
    let key = keyring::Entry::new("studio.voicestudio.app", "daytona-api-key")
        .map_err(|_| "Credential storage unavailable")?
        .get_password()
        .map_err(|_| "No Daytona credential stored")?;
    cloud_command(&app, vec!["cloud".into(), "status".into()], &key).await
}
#[tauri::command]
async fn cloud_stop(app: AppHandle, state: State<'_, NativeState>) -> Result<(), String> {
    let key = keyring::Entry::new("studio.voicestudio.app", "daytona-api-key")
        .map_err(|_| "Credential storage unavailable")?
        .get_password()
        .map_err(|_| "No Daytona credential stored")?;
    cloud_command(&app, vec!["cloud".into(), "stop".into()], &key).await?;
    if connection(&state).is_ok_and(|c| c.mode == "hosted") {
        *state
            .connection
            .lock()
            .map_err(|_| "Connection lock unavailable")? = None;
    }
    Ok(())
}
#[tauri::command]
fn disconnect(state: State<'_, NativeState>) -> Result<(), String> {
    *state
        .connection
        .lock()
        .map_err(|_| "Connection lock unavailable")? = None;
    if let Some(mut child) = state
        .child
        .lock()
        .map_err(|_| "Process lock unavailable")?
        .take()
    {
        terminate(&mut child);
    }
    Ok(())
}
#[tauri::command]
async fn rollback_runtime(app: AppHandle, state: State<'_, NativeState>) -> Result<(), String> {
    let _guard = state
        .install
        .try_lock()
        .map_err(|_| "Runtime installation in progress")?;
    let root = runtime::root(&app)?;
    let lock = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(root.join("runtime-install.lock"))
        .map_err(|_| "Cannot lock runtime installation")?;
    fs2::FileExt::try_lock_exclusive(&lock).map_err(|_| "Runtime installation in progress")?;
    let previous = root.join("runtime.previous.json");
    let metadata: runtime::Manifest =
        serde_json::from_slice(&fs::read(&previous).map_err(|_| "No previous runtime")?)
            .map_err(|_| "Invalid rollback metadata")?;
    runtime::validate(&metadata)?;
    if !root
        .join("runtimes")
        .join(&metadata.version)
        .join(&metadata.executable)
        .is_file()
    {
        return Err("Previous runtime files missing".into());
    }
    if let Ok(c) = connection(&state) {
        let jobs: serde_json::Value = api_builder(&c, "/v1/generations", reqwest::Method::GET)?
            .send()
            .await
            .map_err(|_| "Reconnect before rollback so active jobs can be checked")?
            .error_for_status()
            .map_err(|_| "Reconnect before rollback")?
            .json()
            .await
            .map_err(|_| "Could not inspect jobs")?;
        if jobs.as_array().is_some_and(|rows| {
            rows.iter().any(|job| {
                matches!(
                    job["state"].as_str(),
                    Some("queued" | "starting" | "loading" | "generating" | "processing")
                )
            })
        }) {
            return Err(
                "Wait for active generations to finish or cancel them before rollback".into(),
            );
        }
    }
    let mut next = tempfile::NamedTempFile::new_in(&root).map_err(|_| "Cannot prepare rollback")?;
    use std::io::Write;
    next.write_all(&fs::read(previous).map_err(|_| "Cannot read rollback metadata")?)
        .map_err(|_| "Cannot write rollback metadata")?;
    next.as_file()
        .sync_all()
        .map_err(|_| "Cannot persist rollback metadata")?;
    disconnect(state.clone())?;
    next.persist(root.join("runtime.json"))
        .map_err(|_| "Could not restore previous runtime")?;
    Ok(())
}

#[derive(Default)]
struct PendingUpdate(Mutex<Option<tauri_plugin_updater::Update>>);

#[tauri::command]
async fn check_update(
    app: AppHandle,
    pending: State<'_, PendingUpdate>,
) -> Result<serde_json::Value, String> {
    let key = option_env!("VOICE_UPDATE_PUBLIC_KEY").unwrap_or("");
    let endpoint = option_env!("VOICE_UPDATE_URL").unwrap_or("");
    if key.is_empty() || endpoint.is_empty() {
        return Err("Automatic updates are configured in signed stable releases. This preview can be updated from its source build.".into());
    }
    let url = url::Url::parse(endpoint).map_err(|_| "Invalid release endpoint")?;
    if url.scheme() != "https" {
        return Err("Updates require HTTPS".into());
    }
    let update = app
        .updater_builder()
        .pubkey(key)
        .endpoints(vec![url])
        .map_err(|_| "Invalid release endpoint")?
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|_| "Update configuration is invalid")?
        .check()
        .await
        .map_err(|_| "Could not check for updates")?;
    let result = match &update {
        Some(value) => {
            serde_json::json!({"available": true, "version": value.version, "notes": value.body})
        }
        None => serde_json::json!({"available": false}),
    };
    *pending.0.lock().map_err(|_| "Update lock unavailable")? = update;
    Ok(result)
}

#[tauri::command]
async fn install_update(
    app: AppHandle,
    state: State<'_, NativeState>,
    pending: State<'_, PendingUpdate>,
) -> Result<(), String> {
    if let Ok(c) = connection(&state) {
        let jobs: serde_json::Value = api_builder(&c, "/v1/generations", reqwest::Method::GET)?
            .send()
            .await
            .map_err(|_| "Reconnect before updating so active jobs can be checked")?
            .error_for_status()
            .map_err(|_| "Reconnect before updating")?
            .json()
            .await
            .map_err(|_| "Could not inspect jobs")?;
        if jobs.as_array().is_some_and(|rows| {
            rows.iter().any(|job| {
                matches!(
                    job["state"].as_str(),
                    Some("queued" | "starting" | "loading" | "generating" | "processing")
                )
            })
        }) {
            return Err(
                "Wait for active generations to finish or cancel them before updating".into(),
            );
        }
    }
    let update = pending
        .0
        .lock()
        .map_err(|_| "Update lock unavailable")?
        .take()
        .ok_or("Check for an update first")?;
    disconnect(state)?;
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|_| "The update failed verification or could not be installed")?;
    app.restart();
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(NativeState::default())
        .manage(PendingUpdate::default())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            start_local,
            runtime_status,
            api_request,
            import_voice,
            artifact_bytes,
            save_artifact,
            inspect_runtime,
            install_runtime,
            hosted_deployments,
            hosted_action,
            cloud_start,
            cloud_status,
            cloud_stop,
            disconnect,
            rollback_runtime,
            check_update,
            install_update
        ])
        .build(tauri::generate_context!())
        .expect("Could not initialize Voice Studio")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                let state = app.state::<NativeState>();
                if let Ok(mut guard) = state.child.lock() {
                    if let Some(mut child) = guard.take() {
                        terminate(&mut child);
                    }
                };
            }
        });
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn requests_cannot_change_origin() {
        let base = url::Url::parse("https://private.example/?token=secret").unwrap();
        assert!(api_url(&base, "https://evil.example").is_err());
        assert!(api_url(&base, "/v1/../admin").is_err());
        assert!(api_url(&base, "/v1/%2e%2e/admin").is_err());
        let u = api_url(&base, "/v1/voices").unwrap();
        assert_eq!(u.host_str(), Some("private.example"));
        assert_eq!(u.query(), Some("token=secret"));
    }
}
