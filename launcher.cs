// 木偶工作室启动器（v3.0.1 重建：原源码从未入库，按 CHANGELOG v2.8.0 行为规格重写）
// 编译: csc /target:winexe /win32icon:anon_sewing_icon.ico /out:木偶工作室.exe launcher.cs
// 行为（与 v2.8.0 一致）：
//   1. launcher.ini 的 python= 存在 → 用它
//   2. 同目录 runtime\python.exe（内置便携运行时）
//   3. 常见安装位置（C/D 盘 Python3xx、Program Files）
//   4. PATH 里的 python.exe（排除 WindowsApps 商店别名）
//   5. py -3 启动器问出真实路径
//   探测成功自动写回 launcher.ini；缺关键库弹引导；启动失败弹可操作指引。
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

static class Launcher
{
    static string Dir
    {
        get { return AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\'); }
    }

    [STAThread]
    static void Main()
    {
        string iniPath = Path.Combine(Dir, "launcher.ini");
        string ini = File.Exists(iniPath)
            ? File.ReadAllText(iniPath, Encoding.UTF8) : "";
        string script = IniGet(ini, "script");
        if (string.IsNullOrEmpty(script)) script = "l2d_rig_qt.py";
        if (!Path.IsPathRooted(script))
            script = Path.Combine(Dir, script);

        if (!File.Exists(script))
        {
            MessageBox.Show("找不到主程序脚本：\n" + script +
                "\n\n请把应用完整安装到同一目录。",
                "木偶工作室", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        string py = FindPython(ini, iniPath);
        if (py == null)
        {
            MessageBox.Show(
                "未找到 Python 3.10+。\n\n" +
                "请安装 Python 3.12 后重试，或手动编辑 launcher.ini 的 python= 指向 python.exe。\n\n" +
                "所需依赖（pip 安装）：\n" +
                "  pip install PySide6 PySide6-Fluent-Widgets opencv-python numpy scipy pillow psd-tools\n" +
                "  pip install torch diffusers transformers accelerate peft bitsandbytes scikit-image\n" +
                "（N 卡建议装 CUDA 版 torch，见 pytorch.org）",
                "木偶工作室", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        string missing = CheckDeps(py);
        if (missing != null)
        {
            if (MessageBox.Show(missing, "缺少依赖库",
                    MessageBoxButtons.RetryCancel, MessageBoxIcon.Warning)
                != DialogResult.Retry)
                return;
            missing = CheckDeps(py);
            if (missing != null)
            {
                MessageBox.Show(missing, "仍然缺少依赖库",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
        }

        try
        {
            ProcessStartInfo psi = new ProcessStartInfo(py,
                "\"" + script + "\"");
            psi.WorkingDirectory = Dir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            Process.Start(psi);
        }
        catch (Exception ex)
        {
            MessageBox.Show("启动失败：" + ex.Message, "木偶工作室",
                MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    static string IniGet(string ini, string key)
    {
        foreach (string raw in ini.Split('\n'))
        {
            string line = raw.Trim();
            if (line.StartsWith(key + "="))
                return line.Substring(key.Length + 1).Trim();
        }
        return null;
    }

    static string FindPython(string ini, string iniPath)
    {
        string cand = IniGet(ini, "python");
        if (!string.IsNullOrEmpty(cand) && File.Exists(cand)) return Use(cand, iniPath, ini);

        cand = Path.Combine(Dir, "runtime", "python.exe");
        if (File.Exists(cand)) return Use(cand, iniPath, ini);

        foreach (string root in new[] { @"C:\", @"D:\", @"E:\" })
            foreach (string v in new[] { "312", "311", "310", "313", "39" })
            {
                cand = root + "Python" + v + "\\python.exe";
                if (File.Exists(cand)) return Use(cand, iniPath, ini);
            }
        foreach (string pf in new[] {
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86) })
            foreach (string v in new[] { "312", "311", "310", "313" })
            {
                cand = Path.Combine(pf, "Python" + v, "python.exe");
                if (File.Exists(cand)) return Use(cand, iniPath, ini);
            }

        foreach (string p in (Environment.GetEnvironmentVariable("PATH") ?? "")
                 .Split(';'))
        {
            string t = p.Trim();
            if (t.Length == 0 || t.IndexOf("WindowsApps",
                    StringComparison.OrdinalIgnoreCase) >= 0) continue;
            cand = Path.Combine(t, "python.exe");
            if (File.Exists(cand)) return Use(cand, iniPath, ini);
        }

        try
        {
            ProcessStartInfo psi = new ProcessStartInfo("py",
                "-3 -c \"import sys;print(sys.executable)\"");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            using (Process pr = Process.Start(psi))
            {
                string o = pr.StandardOutput.ReadToEnd().Trim();
                pr.WaitForExit(5000);
                if (pr.ExitCode == 0 && o.EndsWith("python.exe") && File.Exists(o))
                    return Use(o, iniPath, ini);
            }
        }
        catch { }
        return null;
    }

    static string Use(string py, string iniPath, string oldIni)
    {
        try
        {
            if (IniGet(oldIni, "python") != py)
            {
                var sb = new StringBuilder();
                bool hit = false;
                foreach (string raw in (oldIni ?? "").Split('\n'))
                {
                    if (raw.Trim().StartsWith("python="))
                    { sb.AppendLine("python=" + py); hit = true; }
                    else if (raw.Trim().Length > 0) sb.AppendLine(raw.TrimEnd());
                }
                if (!hit) sb.Insert(0, "python=" + py + "\n");
                File.WriteAllText(iniPath, sb.ToString(), Encoding.UTF8);
            }
        }
        catch { }
        return py;
    }

    // 返回 null=齐全；否则返回引导文本
    static string CheckDeps(string py)
    {
        string code = "import importlib.util as u;import sys;" +
            "need=['PySide6','cv2','numpy','torch','diffusers','transformers'];" +
            "m=[n for n in need if not u.find_spec(n)];" +
            "print(','.join(m))";
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo(py, "-c \"" + code + "\"");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            using (Process pr = Process.Start(psi))
            {
                string o = pr.StandardOutput.ReadToEnd().Trim();
                pr.WaitForExit(20000);
                if (pr.ExitCode != 0)
                    return "无法用该 Python 运行检查（可能不是 Python 3）：\n" + py;
                if (o.Length > 0)
                    return "缺少依赖库：" + o + "\n\n请运行：\n  pip install " +
                        o.Replace("PySide6-Fluent-Widgets", "PySide6-Fluent-Widgets");
            }
        }
        catch (Exception ex)
        {
            return "依赖检查失败：" + ex.Message;
        }
        return null;
    }
}
