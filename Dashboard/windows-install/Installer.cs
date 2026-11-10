using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Windows.Forms;

internal static class Installer
{
    private const string Title = "率土战场看板";

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            if (!Environment.Is64BitOperatingSystem)
                throw new InvalidOperationException("此安装包需要 64 位 Windows。");
            if (args.Length == 2 && args[0] == "--extract")
            {
                Install(Path.GetFullPath(args[1]));
                return 0;
            }
            if (args.Length != 0)
                throw new ArgumentException("无法识别的安装参数。");

            string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "STZB-Observer");
            string program = Install(root);
            string launcher = Path.Combine(program, "Start-Installed.ps1");
            string workspace = Path.Combine(root, "Workspace");
            string ps = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe");
            string start = "-NoProfile -ExecutionPolicy Bypass -File \"" + launcher + "\" -Workspace \"" + workspace + "\"";
            CreateShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), Title + ".lnk"), ps, start, program);
            CreateShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), Title + " - 停止.lnk"), ps, start + " -Stop", program);
            Process.Start(new ProcessStartInfo(ps, start) { UseShellExecute = false, CreateNoWindow = true });
            return 0;
        }
        catch (Exception error)
        {
            if (args.Length != 0)
                Console.Error.WriteLine(error.Message);
            else
                MessageBox.Show(error.Message, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static string Install(string root)
    {
        Directory.CreateDirectory(root);
        using (Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("dashboard.zip"))
        using (SHA256 hash = SHA256.Create())
        {
            string version = BitConverter.ToString(hash.ComputeHash(payload)).Replace("-", "").Substring(0, 16);
            string program = Path.Combine(root, "app-" + version);
            if (File.Exists(Path.Combine(program, "Start-Installed.ps1")))
                return program;
            string staging = Path.Combine(root, "setup-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(staging);
            payload.Position = 0;
            using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
                archive.ExtractToDirectory(staging);
            try { Directory.Move(staging, program); }
            catch (IOException)
            {
                if (!File.Exists(Path.Combine(program, "Start-Installed.ps1"))) throw;
                Directory.Delete(staging, true);
            }
            return program;
        }
    }

    private static void CreateShortcut(string filename, string target, string args, string folder)
    {
        object shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
        object link = shell.GetType().InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { filename });
        link.GetType().InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { target });
        link.GetType().InvokeMember("Arguments", BindingFlags.SetProperty, null, link, new object[] { args });
        link.GetType().InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link, new object[] { folder });
        link.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
    }
}
