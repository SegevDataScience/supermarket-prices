# Turns off QuickEdit mode for the console window this script runs in.
# With QuickEdit on, a click inside the window starts a text selection and pauses
# all output, which freezes the server until Esc/Enter is pressed.
$sig = @'
[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
public static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr handle, out uint mode);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr handle, uint mode);
[DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
'@
try {
    $k = Add-Type -MemberDefinition $sig -Name ConsoleQuickEdit -Namespace SupermarketCompare -PassThru
    # CONIN$ is the console's input buffer even if stdin is redirected.
    $h = $k::CreateFileW('CONIN$', 0xC0000000, 3, [IntPtr]::Zero, 3, 0, [IntPtr]::Zero)
    if ($h -eq [IntPtr](-1)) { exit 1 }
    $mode = [uint32]0
    if ($k::GetConsoleMode($h, [ref]$mode)) {
        $ENABLE_QUICK_EDIT_MODE = 0x40
        $ENABLE_EXTENDED_FLAGS = 0x80
        $new = [uint32](($mode -band (-bnot $ENABLE_QUICK_EDIT_MODE)) -bor $ENABLE_EXTENDED_FLAGS)
        [void]$k::SetConsoleMode($h, $new)
    }
    [void]$k::CloseHandle($h)
} catch {
    exit 1
}
