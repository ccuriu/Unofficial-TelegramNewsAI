using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Unofficial TelegramNewsAI")]
[assembly: AssemblyDescription("Запуск Unofficial TelegramNewsAI 5.4.15 Testing")]
[assembly: AssemblyProduct("Unofficial TelegramNewsAI")]
[assembly: AssemblyVersion("5.4.15.0")]
[assembly: AssemblyFileVersion("5.4.15.0")]

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        string folder = AppDomain.CurrentDomain.BaseDirectory;
        string script = Path.Combine(folder, "start_free.bat");
        if (args.Length == 1 && args[0] == "--self-test")
        {
            if (!File.Exists(script)) return 2;
            using (Icon icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath))
                return icon == null ? 3 : 0;
        }
        if (args.Length != 0) return 1;
        if (!File.Exists(script))
        {
            MessageBox.Show(
                "Не найден start_free.bat. Поместите EXE рядом с остальными файлами Unofficial TelegramNewsAI.",
                "Unofficial TelegramNewsAI", MessageBoxButtons.OK, MessageBoxIcon.Information
            );
            return 2;
        }
        try
        {
            string commandProcessor = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System),
                "cmd.exe"
            );
            Process.Start(new ProcessStartInfo
            {
                FileName = commandProcessor,
                Arguments = "/d /s /c \"\"" + script + "\"\"",
                WorkingDirectory = folder,
                UseShellExecute = false,
                CreateNoWindow = false,
                WindowStyle = ProcessWindowStyle.Normal
            });
            return 0;
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, "Не удалось запустить Unofficial TelegramNewsAI",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
