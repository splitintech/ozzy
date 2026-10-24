fn main() {
    println!("cargo:rerun-if-env-changed=VOICE_RUNTIME_PUBLIC_KEY");
    println!("cargo:rerun-if-env-changed=VOICE_UPDATE_PUBLIC_KEY");
    println!("cargo:rerun-if-env-changed=VOICE_UPDATE_URL");
    tauri_build::build()
}
