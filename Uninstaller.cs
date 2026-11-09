// 木偶工作室 卸载器（内置于安装目录）
// 编译: csc /target:winexe /win32icon:laowu_icon.ico /r:System.Management.dll /out:卸载木偶工作室.exe Uninstaller.cs
// 安全铁律：只删"确认是木偶工作室安装目录"内的文件；拒绝盘根/系统目录/含 .git 的源码目录；
//           目录联接(junction)只删链接本身，绝不跟随删除链接目标。
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Management;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

class Uninstaller
{
    static readonly string[] MARKERS = { "launcher.ini", "l2d_rig_qt.py", "木偶工作室.exe" };

    [STAThread]
    static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        if (args.Length >= 2 && args[0] == "--selftest")
        {
            try { Environment.Exit(RunSelfTest(args[1])); }
            catch (Exception ex)
            {
                try { File.WriteAllText(args[1], "SELFTEST CRASH:\n" + ex + "\n\n" + (ex.InnerException != null ? ex.InnerException.ToString() : "")); } catch { }
                Environment.Exit(99);
            }
            return;
        }
        if (args.Length >= 2 && args[0] == "--run")
        {
            bool purge = Array.IndexOf(args, "--purge-output") >= 0;
            bool noPause = Array.IndexOf(args, "--no-pause") >= 0;
            try { DoUninstall(args[1], purge, noPause); }
            catch (Exception ex)
            {
                if (!noPause) MessageBox.Show("卸载异常: " + ex.Message, "错误", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Environment.Exit(1);
            }
            Environment.Exit(0);
            return;
        }
        ShowGui();
    }

    static string AppDir()
    {
        return Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
    }

    // ---------------- 安全校验 ----------------
    static bool IsSafeInstallDir(string dir, out string reason)
    {
        reason = "";
        if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir))
        { reason = "目录不存在"; return false; }
        string full = Path.GetFullPath(dir).TrimEnd('\\', '/');

        // 盘根
        string root = Path.GetPathRoot(full);
        if (full.TrimEnd('\\', '/') == (root ?? "").TrimEnd('\\', '/'))
        { reason = "目标是盘根目录，绝对禁止"; return false; }

        // 系统/个人目录本身
        string[] banned = {
            Environment.GetFolderPath(Environment.SpecialFolder.Windows),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
            Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        };
        foreach (string b in banned)
            if (!string.IsNullOrEmpty(b) &&
                string.Equals(Path.GetFullPath(b).TrimEnd('\\', '/'), full, StringComparison.OrdinalIgnoreCase))
            { reason = "目标是系统/个人目录本身: " + b; return false; }

        // Windows 目录树内一律拒绝
        string win = Environment.GetFolderPath(Environment.SpecialFolder.Windows).TrimEnd('\\');
        if (full.StartsWith(win + "\\", StringComparison.OrdinalIgnoreCase))
        { reason = "目标位于 Windows 目录内，禁止"; return false; }

        // 开发/源码目录保护
        if (Directory.Exists(Path.Combine(full, ".git")))
        { reason = "该目录含 .git（源码/开发目录），卸载器拒绝删除"; return false; }

        // 标志文件必须齐全
        foreach (string m in MARKERS)
            if (!File.Exists(Path.Combine(full, m)))
            { reason = "缺少标志文件「" + m + "」，这不是木偶工作室安装目录"; return false; }

        // launcher.ini 内容核对
        try
        {
            string ini = File.ReadAllText(Path.Combine(full, "launcher.ini"));
            if (!ini.Contains("l2d_rig_qt"))
            { reason = "launcher.ini 不是木偶工作室的配置"; return false; }
        }
        catch (Exception ex) { reason = "无法读取 launcher.ini: " + ex.Message; return false; }
        return true;
    }

    static bool AppRunning()
    {
        try
        {
            using (ManagementObjectSearcher s =
                new ManagementObjectSearcher("select CommandLine from Win32_Process where Name='python.exe'"))
                foreach (ManagementObject o in s.Get())
                {
                    string c = o["CommandLine"] as string;
                    if (c != null && c.Contains("l2d_rig_qt.py")) return true;
                }
        }
        catch { }
        return false;
    }

    // ---------------- 删除核心 ----------------
    // 只删 dir 内部内容；seethrough_out（用户拆分产物）在 !purgeOutput 时保留；
    // 遇到 reparse point（junction/符号链接目录）只删链接本身，绝不进入。
    static long DeleteContents(string dir, bool purgeOutput, List<string> errors)
    {
        long freed = 0;
        foreach (string f in Directory.GetFiles(dir))
        {
            try
            {
                long sz = new FileInfo(f).Length;
                File.SetAttributes(f, FileAttributes.Normal);
                File.Delete(f);
                freed += sz;
            }
            catch (Exception ex) { errors.Add(f + " : " + ex.Message); }
        }
        foreach (string d in Directory.GetDirectories(dir))
        {
            try
            {
                DirectoryInfo di = new DirectoryInfo(d);
                if ((di.Attributes & FileAttributes.ReparsePoint) != 0)
                { di.Delete(); continue; }            // 只删链接，不跟随
                string name = Path.GetFileName(d.TrimEnd('\\', '/'));
                if (!purgeOutput && name.Equals("seethrough_out", StringComparison.OrdinalIgnoreCase))
                    continue;                          // 用户产物默认保留
                freed += DeleteContents(d, purgeOutput, errors);
                if (Directory.GetFileSystemEntries(d).Length == 0)
                    Directory.Delete(d);
            }
            catch (Exception ex) { errors.Add(d + " : " + ex.Message); }
        }
        return freed;
    }

    // 删除指向 installDir 内部的快捷方式（只删目标在本安装目录内的 .lnk）
    static void CleanShortcuts(string installDir, string[] folders, List<string> errors)
    {
        string norm = Path.GetFullPath(installDir).TrimEnd('\\') + "\\";
        Type t = Type.GetTypeFromProgID("WScript.Shell");
        if (t == null) return;
        object obj = Activator.CreateInstance(t);
        foreach (string folder in folders)
        {
            try
            {
                if (!Directory.Exists(folder)) continue;
                foreach (string lnk in Directory.GetFiles(folder, "*.lnk"))
                {
                    try
                    {
                        object sc = t.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, obj, new object[] { lnk });
                        string tgt = (string)sc.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, sc, null);
                        if (!string.IsNullOrEmpty(tgt) && tgt.StartsWith(norm, StringComparison.OrdinalIgnoreCase))
                            File.Delete(lnk);
                    }
                    catch (Exception ex) { errors.Add(lnk + " : " + ex.Message); }
                }
            }
            catch (Exception ex) { errors.Add(folder + " : " + ex.Message); }
        }
    }

    static long DirSize(string dir)
    {
        long sz = 0;
        try
        {
            foreach (string f in Directory.GetFiles(dir)) sz += new FileInfo(f).Length;
            foreach (string d in Directory.GetDirectories(dir))
            {
                DirectoryInfo di = new DirectoryInfo(d);
                if ((di.Attributes & FileAttributes.ReparsePoint) == 0) sz += DirSize(d);
            }
        }
        catch { }
        return sz;
    }

    // ---------------- 实际卸载（从临时副本运行） ----------------
    static void DoUninstall(string dir, bool purge, bool noPause)
    {
        string reason;
        if (!IsSafeInstallDir(dir, out reason))
        {
            if (!noPause) MessageBox.Show("已拒绝卸载：" + reason, "木偶工作室 卸载",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        if (AppRunning())
        {
            if (!noPause) MessageBox.Show("木偶工作室正在运行，请先关闭窗口再卸载。", "木偶工作室 卸载",
                            MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        List<string> errors = new List<string>();
        long freed = DeleteContents(dir, purge, errors);
        CleanShortcuts(dir, new string[] {
            Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
            Environment.GetFolderPath(Environment.SpecialFolder.Programs) }, errors);
        try { if (Directory.GetFileSystemEntries(dir).Length == 0) Directory.Delete(dir); } catch { }

        StringBuilder sb = new StringBuilder();
        sb.AppendLine("卸载完成，释放 " + (freed / 1048576.0).ToString("F1") + " MB。");
        if (!purge && Directory.Exists(Path.Combine(dir, "seethrough_out")))
            sb.AppendLine("保留了你自己的拆分产物: " + Path.Combine(dir, "seethrough_out"));
        else if (Directory.Exists(dir))
            sb.AppendLine("目录未完全清空（可能有文件被占用），残留项:");
        if (errors.Count > 0)
        {
            sb.AppendLine();
            sb.AppendLine("未能删除 " + errors.Count + " 项：");
            for (int i = 0; i < errors.Count && i < 8; i++) sb.AppendLine("  " + errors[i]);
        }
        if (noPause)
        {
            string outPath = Environment.GetEnvironmentVariable("MUPPET_UNINST_REPORT");
            if (!string.IsNullOrEmpty(outPath))
                File.WriteAllText(outPath, sb.ToString(), new UTF8Encoding(false));
            return;
        }
        MessageBox.Show(sb.ToString(), "木偶工作室 卸载", MessageBoxButtons.OK,
                        errors.Count == 0 ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
    }

    // ---------------- GUI ----------------
    static void ShowGui()
    {
        string dir = AppDir();
        string reason;
        bool safe = IsSafeInstallDir(dir, out reason);

        Form f = new Form();
        f.Text = "木偶工作室 卸载";
        f.ClientSize = new System.Drawing.Size(480, 250);
        f.FormBorderStyle = FormBorderStyle.FixedDialog;
        f.MaximizeBox = false; f.MinimizeBox = false;
        f.StartPosition = FormStartPosition.CenterScreen;

        Label l1 = new Label();
        l1.Text = "安装目录：";
        l1.SetBounds(16, 18, 80, 20);
        f.Controls.Add(l1);
        TextBox dirBox = new TextBox();
        dirBox.Text = dir;
        dirBox.ReadOnly = true;
        dirBox.SetBounds(100, 15, 360, 24);
        f.Controls.Add(dirBox);

        Label l2 = new Label();
        l2.Text = safe ? ("总大小约 " + (DirSize(dir) / 1048576.0).ToString("F1") + " MB（含模型）")
                       : ("⚠ 安全校验未通过：" + reason);
        l2.SetBounds(100, 44, 360, 22);
        f.Controls.Add(l2);

        CheckBox chk = new CheckBox();
        chk.Text = "同时删除拆分产物 seethrough_out\\（内含你生成的图层/视频文件）";
        chk.SetBounds(100, 74, 360, 24);
        chk.Checked = false;
        chk.Enabled = safe;
        f.Controls.Add(chk);

        Label l3 = new Label();
        l3.Text = "卸载只删除本目录内的木偶工作室文件与指向它的快捷方式；\r\n" +
                  "盘根、系统目录、源码目录一律拒绝；目录联接不会跟随删除。";
        l3.SetBounds(100, 104, 360, 44);
        f.Controls.Add(l3);

        Button go = new Button();
        go.Text = safe ? "卸 载" : "无法卸载";
        go.Enabled = safe;
        go.SetBounds(130, 180, 100, 34);
        Button quit = new Button();
        quit.Text = "退出";
        quit.SetBounds(250, 180, 100, 34);
        f.Controls.Add(go); f.Controls.Add(quit);

        go.Click += delegate
        {
            if (AppRunning())
            {
                MessageBox.Show(f, "木偶工作室正在运行，请先关闭。", "卸载",
                                MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            DialogResult dr = MessageBox.Show(f,
                "确定卸载木偶工作室？\n\n将删除:\n  " + dir + "\\ 内所有程序文件（含模型）\n  桌面/开始菜单的「木偶工作室」快捷方式\n\n"
                + (chk.Checked ? "将一并删除拆分产物 seethrough_out\\" : "拆分产物 seethrough_out\\ 将保留"),
                "确认卸载", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
            if (dr != DialogResult.Yes) return;
            try
            {
                string tmp = Path.Combine(Path.GetTempPath(),
                    "muppet_uninst_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".exe");
                File.Copy(Assembly.GetExecutingAssembly().Location, tmp, true);
                Process.Start(tmp, "--run \"" + dir + "\"" + (chk.Checked ? " --purge-output" : ""));
                f.Close();
            }
            catch (Exception ex)
            {
                MessageBox.Show(f, "无法启动卸载进程: " + ex.Message, "错误",
                                MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        };
        quit.Click += delegate { f.Close(); };
        Application.Run(f);
    }

    // 建一棵标准"安装目录"树（供自检反复使用；每次调用会先清空重建）
    static void BuildInstallTree(string inst)
    {
        if (Directory.Exists(inst)) Directory.Delete(inst, true);
        Directory.CreateDirectory(inst);
        File.WriteAllText(Path.Combine(inst, "launcher.ini"), "python=D:\\x\\python.exe\nscript=l2d_rig_qt.py\n");
        File.WriteAllText(Path.Combine(inst, "l2d_rig_qt.py"), "#app");
        File.WriteAllText(Path.Combine(inst, "木偶工作室.exe"), "MZ-dummy");
        File.WriteAllText(Path.Combine(inst, "README.md"), "doc");
        Directory.CreateDirectory(Path.Combine(inst, "models"));
        File.WriteAllText(Path.Combine(inst, "models", "a.bin"), "model");
        Directory.CreateDirectory(Path.Combine(inst, "示例工程"));
        File.WriteAllText(Path.Combine(inst, "示例工程", "layers_meta.json"), "{}");
        Directory.CreateDirectory(Path.Combine(inst, "seethrough_out"));
        File.WriteAllText(Path.Combine(inst, "seethrough_out", "user.png"), "userdata");
    }

    // ---------------- 自检（不删任何真实文件，全在临时沙箱） ----------------
    static int RunSelfTest(string logPath)
    {
        List<string> lines = new List<string>();
        int fail = 0;
        string root = Path.Combine(Path.GetTempPath(), "muppet_uninst_test_" + Guid.NewGuid().ToString("N").Substring(0, 8));
        Directory.CreateDirectory(root);
        string inst = Path.Combine(root, "install");
        BuildInstallTree(inst);

        string r;
        // 1) 安全矩阵
        Assert(ref fail, lines, "拒绝盘根", !IsSafeInstallDir(Path.GetPathRoot(inst), out r));
        Assert(ref fail, lines, "拒绝 Windows 目录", !IsSafeInstallDir(Environment.GetFolderPath(Environment.SpecialFolder.Windows), out r));
        Assert(ref fail, lines, "拒绝 Program Files 本身", !IsSafeInstallDir(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), out r));
        Assert(ref fail, lines, "拒绝用户主目录", !IsSafeInstallDir(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), out r));
        Assert(ref fail, lines, "拒绝无标志目录", !IsSafeInstallDir(root, out r));
        string noMarker = Path.Combine(root, "nomarker");
        Directory.CreateDirectory(noMarker);
        Assert(ref fail, lines, "拒绝空目录", !IsSafeInstallDir(noMarker, out r));
        string gitDir = Path.Combine(root, "gitdir");
        Directory.CreateDirectory(Path.Combine(gitDir, ".git"));
        foreach (string m in MARKERS) File.WriteAllText(Path.Combine(gitDir, m), "x");
        File.WriteAllText(Path.Combine(gitDir, "launcher.ini"), "script=l2d_rig_qt.py");
        Assert(ref fail, lines, "拒绝含 .git 的源码目录", !IsSafeInstallDir(gitDir, out r));
        string badIni = Path.Combine(root, "badini");
        Directory.CreateDirectory(badIni);
        foreach (string m in MARKERS) File.WriteAllText(Path.Combine(badIni, m), "x");
        File.WriteAllText(Path.Combine(badIni, "launcher.ini"), "script=别的程序.py");
        Assert(ref fail, lines, "拒绝 ini 内容不符", !IsSafeInstallDir(badIni, out r));
        Assert(ref fail, lines, "接受合法安装目录", IsSafeInstallDir(inst, out r));

        // 2) junction 不跟随（独立沙箱，避免干扰后续）
        string jroot = Path.Combine(root, "junction_case");
        Directory.CreateDirectory(jroot);
        string jinst = Path.Combine(jroot, "install");
        BuildInstallTree(jinst);
        string prot = Path.Combine(jroot, "protected");
        Directory.CreateDirectory(prot);
        File.WriteAllText(Path.Combine(prot, "keep.txt"), "precious");
        string link = Path.Combine(jinst, "linkdir");
        ProcessStartInfo psi = new ProcessStartInfo("cmd.exe", "/c mklink /J \"" + link + "\" \"" + prot + "\"");
        psi.CreateNoWindow = true;
        bool mkOk = false;
        try { mkOk = Process.Start(psi).WaitForExit(5000); } catch { }
        if (mkOk && Directory.Exists(link))
        {
            List<string> errs = new List<string>();
            DeleteContents(jinst, false, errs);
            Assert(ref fail, lines, "junction 只删链接", !Directory.Exists(link));
            Assert(ref fail, lines, "junction 目标毫发无损", File.Exists(Path.Combine(prot, "keep.txt")));
        }
        else
        {
            lines.Add("SKIP junction 测试（mklink 不可用）");
        }

        // 3) 删除语义：保留用户产物（重建干净树）
        BuildInstallTree(inst);
        List<string> e2 = new List<string>();
        long freed = DeleteContents(inst, false, e2);
        Assert(ref fail, lines, "程序文件被删", !File.Exists(Path.Combine(inst, "README.md")));
        Assert(ref fail, lines, "models 被删", !Directory.Exists(Path.Combine(inst, "models")));
        Assert(ref fail, lines, "seethrough_out 保留", File.Exists(Path.Combine(inst, "seethrough_out", "user.png")));
        Assert(ref fail, lines, "freed>0", freed > 0);

        // 4) purge 模式
        BuildInstallTree(inst);
        List<string> e3 = new List<string>();
        DeleteContents(inst, true, e3);
        Assert(ref fail, lines, "purge 删除产物", !Directory.Exists(Path.Combine(inst, "seethrough_out")));

        // 5) 快捷方式清理：只删指向安装目录的
        BuildInstallTree(inst);
        string desk = Path.Combine(root, "fake_desk");
        Directory.CreateDirectory(desk);
        Type wt = Type.GetTypeFromProgID("WScript.Shell");
        object sh = Activator.CreateInstance(wt);
        object s1 = wt.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, sh,
            new object[] { Path.Combine(desk, "木偶工作室.lnk") });
        s1.GetType().InvokeMember("TargetPath", BindingFlags.SetProperty, null, s1,
            new object[] { Path.Combine(inst, "木偶工作室.exe") });
        s1.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, s1, null);
        object s2 = wt.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, sh,
            new object[] { Path.Combine(desk, "记事本.lnk") });
        s2.GetType().InvokeMember("TargetPath", BindingFlags.SetProperty, null, s2,
            new object[] { Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32", "notepad.exe") });
        s2.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, s2, null);
        List<string> e4 = new List<string>();
        CleanShortcuts(inst, new string[] { desk }, e4);
        Assert(ref fail, lines, "指向安装目录的快捷方式被删", !File.Exists(Path.Combine(desk, "木偶工作室.lnk")));
        Assert(ref fail, lines, "无关快捷方式保留", File.Exists(Path.Combine(desk, "记事本.lnk")));

        // 清理沙箱
        try { Directory.Delete(root, true); } catch { }
        int code = fail;
        lines.Add("======== " + (fail == 0 ? "ALL PASS" : fail + " FAIL") + " ========");
        File.WriteAllLines(logPath, lines.ToArray(), new UTF8Encoding(false));
        return code;
    }

    static void Assert(ref int fail, List<string> lines, string name, bool ok)
    {
        lines.Add((ok ? "PASS " : "FAIL ") + name);
        if (!ok) fail++;
    }
}
