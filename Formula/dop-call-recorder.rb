# Template: @URL@ / @SHA256@ are filled in by scripts/tap-install-local.sh (local tests) or by the release process.
class DopCallRecorder < Formula
  desc "Call auto-recorder and offline Whisper transcriber for macOS (DevOps Pass)"
  homepage "https://github.com/devopspass/dop-call-recorder"
  license "MIT"
  url "@URL@"
  sha256 "@SHA256@"

  depends_on arch: :arm64
  depends_on "ffmpeg"
  depends_on macos: :sonoma
  depends_on "python@3.13"

  def install
    odie "Xcode Command Line Tools are required to build the apps: run `xcode-select --install`" unless quiet_system("xcrun", "--find", "swiftc")
    python = formula_opt_bin("python@3.13")/"python3.13"
    venv = libexec/"venv"
    system python, "-m", "venv", venv
    pip = [venv/"bin/python", "-m", "pip", "--disable-pip-version-check", "install"]
    # mlx-whisper metadata hard-requires torch (never imported): install from the hash-pinned lock, no resolver.
    system(*pip, "--no-deps", "--require-hashes", "-r", "requirements.lock")
    system(*pip, "--no-deps", ".")

    apps = libexec/"apps"
    (libexec/"bin").mkpath
    with_env("OUT_DIR" => apps.to_s) do
      system "app/build.sh"
    end
    with_env("OUT_DIR" => (libexec/"bin").to_s) { system "scripts/build-probe.sh" }
    (libexec/"bin").install "app/bin/recorder"
    prefix.install "config.toml", "vocabulary.yaml"
    pkgshare.install "tests"

    # tiktoken's extension is built as MH_DYLIB with an LC_ID_DYLIB; Homebrew would rewrite that id to a path that
    # does not fit the Mach-O header. Python extensions load fine as MH_BUNDLE without an id, which Homebrew skips.
    Dir[venv/"**/*.so"].each do |f|
      data = File.binread(f)
      next if data.unpack1("@12V") != 6

      ncmds, sizeofcmds = data.unpack("@16VV")
      off = 32
      ncmds.times do
        cmd, size = data.unpack("@#{off}VV")
        if cmd == 0xd
          data[off, size] = ""
          data.insert(32 + sizeofcmds - size, "\0" * size)
          data[12, 4] = "\b\0\0\0"
          data[16, 8] = [ncmds - 1, sizeofcmds - size].pack("l<l<")
          File.binwrite(f, data)
          system "codesign", "-f", "-s", "-", f
          break
        end
        off += size
      end
    end

    (bin/"dop-recorder").write_env_script venv/"bin/dop-recorder",
                                          DOP_HOME: opt_prefix, HOMEBREW_PREFIX: HOMEBREW_PREFIX
  end

  def caveats
    <<~EOS
      One-time setup per user (downloads the Whisper model, copies the apps to ~/Applications, asks for macOS permissions):
        dop-recorder setup
      Then start recording in the background (agent + menu-bar icon; add --no-menubar for no icon):
        dop-recorder start

      The apps are ad-hoc signed: after every `brew upgrade` macOS forgets their permissions.
      Re-enable the "DOP Call Recorder" app in System Settings > Privacy & Security (Microphone,
      Screen & System Audio Recording, Accessibility), `dop-recorder start` tells you when needed.
      Remove everything: dop-recorder uninstall [--purge] [--purge-models]; brew uninstall dop-call-recorder
    EOS
  end

  test do
    ENV["DOP_STATE_DIR"] = testpath.to_s
    assert_match version.to_s, shell_output("#{bin}/dop-recorder --version")
    # guards the Mach-O patch applied to extension modules in install
    system libexec/"venv/bin/python", "-c", "import tiktoken, mlx_whisper"
    # full unit-test suite against the installed package (pytest is installed on demand, not part of the runtime venv)
    system libexec/"venv/bin/python", "-m", "pip", "--disable-pip-version-check", "install",
           "--target", testpath/"pytestlib", "pytest"
    cp_r pkgshare/"tests", testpath/"tests"
    cp prefix/"config.toml", testpath
    cp prefix/"vocabulary.yaml", testpath
    with_env("PYTHONPATH" => (testpath/"pytestlib").to_s, "DOP_HOME" => opt_prefix.to_s) do
      system libexec/"venv/bin/python", "-m", "pytest", "-q", "-x", testpath/"tests"
    end
    assert_match "plist OK", shell_output("#{bin}/dop-recorder start --dry-run", 1)
  end
end
