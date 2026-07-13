on run
  set appPath to POSIX path of (path to me)
  set launcherPath to appPath & "/Contents/Resources/launcher.sh"
  tell application "Terminal"
    activate
    «event coredosc» (quoted form of launcherPath)
  end tell
end run
