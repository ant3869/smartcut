// SmartCut.exe: the double-clickable front door for pipeline/launcher.py.
// install-desktop.ps1 builds it with the C# compiler that ships with Windows, so it
// needs no SDK. It lives in the project root and runs the project's own venv; the
// Python launcher owns the server and window lifecycle.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

static class SmartCut
{
    [STAThread]
    static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        if (!File.Exists(pythonw))
        {
            MessageBox.Show("SmartCut is not set up yet.\n\nRun setup.ps1 in " + root + " first.",
                "SmartCut", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        string forwarded = string.Join(" ", args.Select(a => "\"" + a.Replace("\"", "\\\"") + "\""));
        Process.Start(new ProcessStartInfo(pythonw, ("-m pipeline.launcher " + forwarded).Trim())
        {
            WorkingDirectory = root,
            UseShellExecute = false,
        });
        return 0;
    }
}
