// 木偶工作室 安装器（支持 更新安装 / 全新安装 两种模式）
// 编译: csc /target:winexe /win32icon:anon_sewing_icon.ico
//       /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll
//       /resource:muppet_app.zip /resource:model_manifest.json installer.cs
// CLI（自动化测试用）: --detect | --update <dir> [--no-pause] | --fresh <dir> [--models] [--no-pause]
// 行为: 更新=校验旧安装目录(标志文件)后仅覆盖程序文件，models/ 与 seethrough_out/ 绝不动；
//       全新=选目录解压，可选下载 nf4 模型(5.3GB 断点续传+校验+双镜像)。
//       两种模式都把安装路径写入 HKCU\Software\MuppetStudio 供下次检测。
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Management;
using System.Net.NetworkInformation;
using Microsoft.Win32;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using System.Drawing;

class Installer
{
    const string REG_KEY = "Software\\MuppetStudio";
    static readonly string[] MARKERS = { "launcher.ini", "l2d_rig_qt.py" };

    Form win;
    TextBox dirBox;
    RadioButton radioUpdate, radioFresh;
    Label detectLbl;
    CheckBox modelChk;
    Label gpuLbl;
    CheckBox amdChk;
    string gpuV = "unknown", gpuN = "未检测到";
    ProgressBar bar;
    Label status;
    Button browseBtn, installBtn;
    Button repairBtn;
    string appZip, manifest;
    string detected = "";
    volatile bool running = false;
    volatile bool cancelFlag = false;

    [STAThread]
    static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        // .NET 4.0 默认 TLS1.0，镜像要求 1.2/1.3 -> 显式打开（int 强转兼容旧枚举）
        try
        {
            ServicePointManager.SecurityProtocol =
                (SecurityProtocolType)(4080 | 3072 | 192 | 768);   // TLS1.3+1.2+1.1+1.0，纯 int 兼容旧枚举
            ServicePointManager.DefaultConnectionLimit = 4;
            ServicePointManager.Expect100Continue = false;
        }
        catch { }
        Installer inst = new Installer();
        inst.LoadResources();

        List<string> a = new List<string>(args);
        bool noPause = a.Contains("--no-pause");
        bool relaunched = a.Contains("--relaunched");

        if (a.Count >= 1 && a[0] == "--gpu")
        {
            string nm; string v = GpuVendor(out nm);
            Console.WriteLine(v + " | " + nm);
            Environment.Exit(0);
            return;
        }
        if (a.Count >= 1 && a[0] == "--diag")
        {
            Console.WriteLine(DiagnoseNetwork());
            Environment.Exit(0);
            return;
        }
        if (a.Count >= 1 && a[0] == "--probe")
        {
            string err = inst.CheckLinkAvailable();
            Console.WriteLine(err == null ? "LINK_OK" : ("LINK_FAIL\n" + err));
            Environment.Exit(err == null ? 0 : 1);
            return;
        }
        if (a.Count >= 2 && a[0] == "--probe-url")
        {
            // 测试钩子：直接探测指定 URL（验证失败分类，不动真实镜像）
            string err = ProbeUrl(a[1]);
            Console.WriteLine(err == null ? "URL_OK" : ("URL_FAIL " + err));
            Environment.Exit(err == null ? 0 : 1);
            return;
        }
        if (a.Count >= 1 && a[0] == "--detect")
        {
            string d = DetectInstall();
            Console.WriteLine(string.IsNullOrEmpty(d) ? "NONE" : d);
            Environment.Exit(0);
            return;
        }
        if (a.Count >= 2 && a[0] == "--update")
        {
            bool withModels = a.Contains("--models");
            int code = inst.DoUpdate(a[1], withModels, noPause, a.Contains("--amd"));
            Environment.Exit(code);
            return;
        }
        if (a.Count >= 2 && a[0] == "--fresh")
        {
            bool withModels = a.Contains("--models");
            int code = inst.DoFresh(a[1], withModels, noPause, a.Contains("--amd"));
            Environment.Exit(code);
            return;
        }
        if (a.Count >= 2 && a[0] == "--check-models")
        {
            List<string> bad = inst.FindBadModels(a[1]);
            Console.WriteLine(bad.Count == 0 ? "COMPLETE" : ("BAD=" + bad.Count));
            foreach (string b in bad) Console.WriteLine("  " + b);
            Environment.Exit(bad.Count == 0 ? 0 : 1);
            return;
        }
        if (a.Count >= 2 && a[0] == "--repair")
        {
            string d = a[1];
            if (!Directory.Exists(d)) { Console.WriteLine("NO_DIR"); Environment.Exit(2); return; }
            List<string> uerr = new List<string>();
            long written = inst.ExtractOverwrite(inst.appZip, d, uerr);
            Console.WriteLine("UPDATED=" + written + " ERR=" + uerr.Count);
            int moved = 0;
            List<string> bad = inst.RepairModels(d, out moved);
            Console.WriteLine("MOVED=" + moved + " BAD=" + bad.Count);
            foreach (string b in bad) Console.WriteLine("  " + b);
            Environment.Exit(bad.Count == 0 ? 0 : 1);
            return;
        }
        inst.ShowGui(relaunched);
    }

    void LoadResources()
    {
        Assembly asm = Assembly.GetExecutingAssembly();
        byte[] zip;
        using (Stream s = asm.GetManifestResourceStream("muppet_app.zip"))
        {
            zip = new byte[s.Length];
            s.Read(zip, 0, zip.Length);
        }
        using (Stream s = asm.GetManifestResourceStream("model_manifest.json"))
        using (StreamReader r = new StreamReader(s, Encoding.UTF8))
            manifest = r.ReadToEnd();
        appZip = Path.Combine(Path.GetTempPath(), "muppet_app_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".zip");
        File.WriteAllBytes(appZip, zip);
    }

    // ---------------- 旧版本检测 ----------------
    static bool HasMarkers(string dir)
    {
        if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir)) return false;
        if (!File.Exists(Path.Combine(dir, "l2d_rig_qt.py"))) return false;
        string ini = Path.Combine(dir, "launcher.ini");
        if (File.Exists(ini))
        {
            try { return File.ReadAllText(ini).Contains("l2d_rig_qt"); } catch { }
        }
        // 无 launcher.ini 的更早版本：认主程序 + exe 双标志
        return File.Exists(Path.Combine(dir, "木偶工作室.exe"));
    }

    static string DetectInstall()
    {
        try
        {
            using (RegistryKey k = Registry.CurrentUser.OpenSubKey(REG_KEY))
            {
                if (k != null)
                {
                    string p = k.GetValue("InstallPath") as string;
                    if (HasMarkers(p)) return p;
                }
            }
        }
        catch { }
        // 注册表没有：扫常见安装位置
        List<string> cands = new List<string>();
        foreach (Environment.SpecialFolder f in new Environment.SpecialFolder[] { Environment.SpecialFolder.ProgramFiles, Environment.SpecialFolder.CommonProgramFiles, Environment.SpecialFolder.LocalApplicationData })
        {
            try
            {
                string root = Environment.GetFolderPath(f);
                cands.Add(Path.Combine(root, "木偶工作室"));
                cands.Add(Path.Combine(root, "MuppetStudio"));
                cands.Add(Path.Combine(root, "Programs", "木偶工作室"));
                cands.Add(Path.Combine(root, "Programs", "MuppetStudio"));
            }
            catch { }
        }
        foreach (string drive in new string[] { "C:", "D:", "E:" })
        {
            cands.Add(drive + "\\木偶工作室");
            cands.Add(drive + "\\MuppetStudio");
            cands.Add(drive + "\\Program Files\\木偶工作室");
        }
        foreach (string c in cands)
            if (HasMarkers(c)) return c;
        return "";
    }

    // 更新目标必须"是旧安装目录"且不是危险路径
    static bool CheckUpdateTarget(string dir, out string reason)
    {
        reason = "";
        dir = Path.GetFullPath(dir).TrimEnd('\\', '/');
        string root = Path.GetPathRoot(dir);
        if (dir == (root ?? "").TrimEnd('\\', '/')) { reason = "盘根目录"; return false; }
        if (!HasMarkers(dir))
        { reason = "该目录不含木偶工作室旧版文件（缺 l2d_rig_qt.py / launcher.ini 标志）"; return false; }
        return true;
    }

    static bool AppRunning()
    {
        try
        {
            using (ManagementObjectSearcher s =
                new ManagementObjectSearcher("select CommandLine,Name from Win32_Process where Name='python.exe' or Name='木偶工作室.exe'"))
                foreach (ManagementObject o in s.Get())
                {
                    string name = o["Name"] as string;
                    string c = o["CommandLine"] as string;
                    if (name == "木偶工作室.exe") return true;
                    if (c != null && c.Contains("l2d_rig_qt.py")) return true;
                }
        }
        catch { }
        return false;
    }

    // ---------------- 解压（覆盖式，绝不动 models / seethrough_out） ----------------
    long ExtractOverwrite(string zipPath, string dest, List<string> errors)
    {
        long written = 0;
        using (ZipArchive z = ZipFile.OpenRead(zipPath))
        {
            int total = z.Entries.Count, i = 0;
            foreach (ZipArchiveEntry e in z.Entries)
            {
                i++;
                string rel = e.FullName.Replace('\\', '/');
                // 5.3GB nf4 模型与用户产物绝不覆盖；但 models/sam/（mobile_sam 内置权重）允许安装/更新
                if (rel.StartsWith("models/24yearsold/") || rel.StartsWith("seethrough_out/") || rel.Length == 0)
                    continue;
                string full = Path.GetFullPath(Path.Combine(dest, e.FullName));
                string destNorm = Path.GetFullPath(dest) + Path.DirectorySeparatorChar;
                if (!full.StartsWith(destNorm, StringComparison.OrdinalIgnoreCase))
                    continue;                       // 防 zip slip
                if (e.FullName.EndsWith("/"))
                {
                    Directory.CreateDirectory(full);
                    continue;
                }
                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(full));
                    // 杀软/索引器可能短暂锁住刚写出的 exe，重试 5 次
                    bool done = false;
                    for (int attempt = 0; attempt < 5 && !done; attempt++)
                    {
                        try
                        {
                            if (File.Exists(full)) File.SetAttributes(full, FileAttributes.Normal);
                            e.ExtractToFile(full, true);
                            done = true;
                        }
                        catch (IOException) { Thread.Sleep(400 * (attempt + 1)); }
                    }
                    if (!done) { errors.Add(e.FullName + " : 文件被占用"); continue; }
                    written++;
                }
                catch (Exception ex) { errors.Add(e.FullName + " : " + ex.Message); }
                if (i % 20 == 0) ReportProgress(i * 30L / total, 300);
            }
        }
        ReportProgress(300, 300);
        return written;
    }

    // ---------------- 更新模式 ----------------
    int DoUpdate(string dir, bool withModels, bool noPause, bool withAmd = false)
    {
        string reason;
        if (!CheckUpdateTarget(dir, out reason))
        {
            Report(noPause, "已拒绝更新：" + reason);
            return 2;
        }
        if (AppRunning())
        {
            Report(noPause, "木偶工作室正在运行，请先关闭再更新");
            return 3;
        }
        SetStatus("正在覆盖程序文件（保留模型与用户产物）…");
        List<string> errors = new List<string>();
        long written = ExtractOverwrite(appZip, dir, errors);
        WriteRegistry(dir);
        try { MakeShortcuts(dir); } catch { }
        string msg = "更新完成：覆盖 " + written + " 个程序文件。\n" +
                     "models\\（模型）与 seethrough_out\\（你的产物）未改动。";

        // 更新时模型处理（三分支）：
        //   1) 完整 -> 走老路，绝不动 models 目录
        //   2) 不完整（缺失/截断）-> 只重下坏文件（DownloadModels 跳过大小正确的）
        //   3) 完全没有 -> 勾选了就下，没勾选只提示
        if (HasModels(dir))
        {
            List<string> bad = FindBadModels(dir);
            if (bad.Count == 0)
            {
                msg += "\n模型完整性检查通过，未改动。";
            }
            else
            {
                string linkErr2 = CheckLinkAvailable();
                if (linkErr2 != null)
                {
                    msg += "\n\n检测到模型不完整（" + bad.Count + " 个文件损坏/缺失），"
                           + "但当前无法连接下载源，本次未修复。可稍后联网重跑安装器自动修复。";
                }
                else
                {
                    SetStatus("检测到模型不完整，正在修复（重下 " + bad.Count + " 个文件）…");
                    try
                    {
                        DownloadModels(dir);
                        List<string> stillBad = FindBadModels(dir);
                        if (stillBad.Count == 0)
                            msg += "\n模型已修复（重下 " + bad.Count + " 个文件）。";
                        else
                            errors.Add("模型修复后仍有 " + stillBad.Count + " 个文件不完整");
                    }
                    catch (Exception ex) { errors.Add("模型修复: " + ex.Message); }
                }
            }
        }
        else if (withModels)
        {
            string linkErr2 = CheckLinkAvailable();
            if (linkErr2 != null)
            {
                msg += "\n\n模型下载失败：无法连接模型下载源。\n" + linkErr2
                       + "\n可稍后联网时重新运行安装器补装。";
            }
            else
            {
                try { DownloadModels(dir); msg += "\n模型已下载就位。"; }
                catch (Exception ex) { errors.Add("模型: " + ex.Message); }
            }
        }
        else
        {
            msg += "\n\n提示：未检测到模型（5.3GB），拆分功能不可用；"
                   + "可重新运行安装器勾选下载模型。";
        }
        if (withAmd)
        {
            List<string> aerrs = DownloadAmdExtras(dir);
            foreach (string e in aerrs) errors.Add(e);
            msg += aerrs.Count == 0
                ? "\n\nA 卡资产已就位：comfyui_setup\\（ComfyUI 安装器 + See-through 插件 + 指引）"
                : "\n\nA 卡资产部分失败（见下方失败项，可重跑安装器补下，支持断点续传）";
        }
        if (errors.Count > 0)
            msg += "\n失败 " + errors.Count + " 项: " + errors[0];
        Report(noPause, msg);
        return errors.Count == 0 ? 0 : 1;
    }

    // 模型"已装好"判定。
    // 早期版本只检查"某个 unet 权重文件存在"，导致**残缺模型也被判为已装**
    // （实测一个只有 7 个文件 / 0.01GB 的目录被判"模型已下载就位"，实际缺 32 个文件）。
    // 现在走 FindBadModels 全量校验：manifest 里每个文件都存在且大小正确才算装好。
    bool HasModels(string dir)
    {
        try
        {
            if (!Directory.Exists(Path.Combine(dir, "models", "24yearsold"))) return false;
            List<string> bad = FindBadModels(dir);
            return (bad == null || bad.Count == 0);
        }
        catch { return false; }
    }

    static char SEP = Path.DirectorySeparatorChar;

    // 模型落盘路径：dir/models/<repo>/<file>
    // repo 形如 24yearsold/seethroughv0.0.1_marigold_nf4 -> 完整路径
    //   dir/models/24yearsold/seethroughv0.0.1_marigold_nf4/<file>
    // 必须与 HasModels 和应用加载路径（seethrough_local: MODELS_DIR + NF4_*）一致。
    // 旧 DownloadModels 把 file 直接拼到 models/24yearsold/ 顶层（漏 repo 子目录），
    // 会让下载落点与应用加载点不符 —— 统一走这里。
    static string ModelDest(string dir, Dict f)
    {
        return Path.Combine(dir, "models",
            f["repo"].Replace('/', SEP), f["file"].Replace('/', SEP));
    }

    // 模型完整性检查（size 级）。
    // manifest 只有 repo/file/size 三字段（打包时从本地模型扫描生成，无哈希），
    // 所以只能校验大小 —— 足以抓"下载中断导致的截断"（用户核心场景），
    // 抓不了"大小恰好相同的字节损坏"。
    // DownloadModels 会跳过大小正确的、覆盖重下大小不符的，坏列表非空时调它即修复。
    List<string> FindBadModels(string dir)
    {
        var bad = new List<string>();
        foreach (Dict f in ParseManifest())
        {
            string dest = ModelDest(dir, f);
            long size;
            if (!long.TryParse(f["size"], out size)) size = -1;
            string tag = f["repo"] + "/" + f["file"];
            if (!File.Exists(dest)) { bad.Add(tag); continue; }
            try { if (new FileInfo(dest).Length != size) bad.Add(tag); }
            catch { bad.Add(tag); }
        }
        return bad;
    }

    // 修复模型目录：把"平铺"的模型文件归位到正确的 repo 子目录，再报告缺失项。
    // 背景：旧版 DownloadModels 把 file 直接写到 models/24yearsold/ 顶层（漏 repo 子目录），
    // 而两个 repo 有大量同名文件（model_index.json / unet/... / LICENSE…）会互相覆盖，
    // 同时 HasModels 查的是"带 repo 子目录"的路径 -> 永远判定未安装 -> 每次安装都重下，
    // 表现为"读不到下载内容却一直在下载"。修复 = 归位 + 只补缺失。
    // 返回归位后仍缺失/损坏的文件标签列表；moved 输出归位文件数。
    List<string> RepairModels(string dir, out int moved)
    {
        moved = 0;
        string flatRoot = Path.Combine(dir, "models", "24yearsold");
        List<Dict> files = ParseManifest();
        // 建立 "相对路径 -> 应属于的 repo 短名列表"，用于判断平铺文件归属
        var byRel = new Dictionary<string, List<string>>();
        foreach (Dict f in files)
        {
            string rel = f["file"];
            if (!byRel.ContainsKey(rel)) byRel[rel] = new List<string>();
            string full = f["repo"];
            int cut = full.LastIndexOf('/');
            byRel[rel].Add(cut >= 0 ? full.Substring(cut + 1) : full);
        }
        // 1) 平铺文件归位：models/24yearsold/<rel> -> models/24yearsold/<repoShort>/<rel>
        //    rel 在多个 repo 里同名时（model_index.json / LICENSE 等），不能靠名字判断归属，
        //    改用**大小匹配**：候选 repo 里只有一个的大小与平铺文件一致才搬。
        //    （manifest 只有 size，没有哈希；大小一致已能覆盖绝大多数情形。）
        try
        {
            if (Directory.Exists(flatRoot))
            {
                Dictionary<string, long> sizeOf = new Dictionary<string, long>();
                foreach (Dict f in files)
                {
                    string full = f["repo"];
                    int cut = full.LastIndexOf('/');
                    string shortName = cut >= 0 ? full.Substring(cut + 1) : full;
                    long sz;
                    long.TryParse(f["size"], out sz);
                    sizeOf[shortName + "|" + f["file"]] = sz;
                }
                foreach (string flat in Directory.GetFiles(flatRoot, "*", SearchOption.AllDirectories))
                {
                    string rel = flat.Substring(flatRoot.Length).TrimStart(SEP);
                    if (rel.StartsWith("seethroughv")) continue;   // 已在 repo 子目录里，跳过
                    List<string> owners;
                    if (!byRel.TryGetValue(rel, out owners)) continue;
                    long actual;
                    try { actual = new FileInfo(flat).Length; } catch { continue; }
                    // 用大小在候选 repo 里筛出唯一匹配
                    string pick = null;
                    int hits = 0;
                    foreach (string repoShort in owners)
                    {
                        long want2;
                        if (sizeOf.TryGetValue(repoShort + "|" + rel, out want2) && want2 == actual)
                        { pick = repoShort; hits++; }
                    }
                    if (hits != 1) continue;                        // 无法唯一确定，留给重新下载
                    string want = ModelDest(dir, MakeDict(pick, rel));
                    if (string.Equals(Path.GetFullPath(flat), Path.GetFullPath(want),
                                      StringComparison.OrdinalIgnoreCase)) continue;
                    try
                    {
                        Directory.CreateDirectory(Path.GetDirectoryName(want));
                        if (!File.Exists(want)) { File.Move(flat, want); moved++; }
                        else { File.Delete(flat); moved++; }
                    }
                    catch { }
                }
                // 递归搬完后清掉空的中间目录（如 top-level unet/）
                try
                {
                    foreach (string dn in Directory.GetDirectories(flatRoot))
                    {
                        string name = Path.GetFileName(dn);
                        if (name.StartsWith("seethroughv")) continue;
                        if (Directory.GetFiles(dn, "*", SearchOption.AllDirectories).Length == 0)
                            try { Directory.Delete(dn, true); } catch { }
                    }
                }
                catch { }
            }
        }
        catch { }
        // 2) 清掉下载中断残留的 .part
        try
        {
            if (Directory.Exists(flatRoot))
                foreach (string pz in Directory.GetFiles(flatRoot, "*.part", SearchOption.AllDirectories))
                    try { File.Delete(pz); } catch { }
        }
        catch { }
        // 3) 归位后重新算缺失（缺 + 大小不符）
        return FindBadModels(dir);
    }

    static Dict MakeDict(string repoShort, string rel)
    {
        Dict d = new Dict();
        d["repo"] = "24yearsold/" + repoShort;
        d["file"] = rel;
        return d;
    }

    // 只补缺失：现有 DownloadModels 会遍历 manifest 但跳过大小正确的，
    // 所以直接调它 —— 缺失文件必然被下载，已完整的不会重下。
    void DownloadOnlyMissing(string dir, List<string> bad)
    {
        if (bad == null || bad.Count == 0) return;
        DownloadModels(dir);
    }

    // ---------------- 显卡检测：安装前自动选路线 ----------------
    // 只读 WMI，无副作用；任何异常都回退 nvidia/unknown（不阻断老流程）。
    // 双显卡：有 N 卡优先 N 卡（本地引擎可直接拆图），避免误下 172MB ComfyUI。
    static string GpuVendor(out string gpuName)
    {
        gpuName = "未检测到";
        // 测试钩子：MUPPET_FAKE_GPU=amd|nvidia|other 强制返回（仅用于 A 卡路线验证）
        string fake = Environment.GetEnvironmentVariable("MUPPET_FAKE_GPU");
        if (!string.IsNullOrEmpty(fake))
        {
            gpuName = "（模拟）" + fake;
            return fake;
        }
        bool hasNv = false, hasAmd = false;
        string nv = null, amd = null, other = null;
        try
        {
            using (ManagementObjectSearcher s =
                new ManagementObjectSearcher("select Name,AdapterCompatibility from Win32_VideoController"))
                foreach (ManagementObject o in s.Get())
                {
                    string nm = (o["Name"] as string) ?? "";
                    string cp = (o["AdapterCompatibility"] as string) ?? "";
                    string all = (nm + " " + cp).ToLower();
                    if (all.Contains("nvidia")) { hasNv = true; if (nv == null) nv = nm; }
                    else if (all.Contains("amd") || all.Contains("radeon") || all.Contains("advanced micro"))
                    { hasAmd = true; if (amd == null) amd = nm; }
                    else if (other == null) other = nm.Trim();
                }
        }
        catch { }
        if (hasNv) { gpuName = nv; return "nvidia"; }
        if (hasAmd) { gpuName = amd; return "amd"; }
        if (other != null && other.Length > 0) { gpuName = other; return "other"; }
        return "unknown";
    }

    // 通用断点续传下载（多镜像依次尝试）
    static bool DownloadToFile(string[] urls, string dest, long minBytes, Action<string> progress)
    {
        string part = dest + ".part";
        foreach (string url in urls)
        {
            try
            {
                long have = File.Exists(part) ? new FileInfo(part).Length : 0;
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
                req.UserAgent = "Mozilla/5.0";
                req.Timeout = 30000;
                req.ReadWriteTimeout = 60000;
                if (have > 0) req.AddRange(have);
                using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                {
                    long total = resp.ContentLength > 0 ? resp.ContentLength + have : -1;
                    if (have > 0 && resp.StatusCode != HttpStatusCode.PartialContent)
                    { have = 0; if (File.Exists(part)) File.Delete(part); }
                    using (Stream net = resp.GetResponseStream())
                    using (FileStream fs = new FileStream(part, have > 0 ? FileMode.Append : FileMode.Create,
                                                          FileAccess.Write, FileShare.None, 1 << 20))
                    {
                        byte[] buf = new byte[1 << 20];
                        int n; long got = have;
                        while ((n = net.Read(buf, 0, buf.Length)) > 0)
                        {
                            fs.Write(buf, 0, n);
                            got += n;
                            if (progress != null && total > 0) progress(got / 1048576 + "/" + total / 1048576 + " MB");
                        }
                    }
                }
                long sz = new FileInfo(part).Length;
                if (sz >= minBytes)
                {
                    if (File.Exists(dest)) File.Delete(dest);
                    File.Move(part, dest);
                    return true;
                }
            }
            catch { }
        }
        return false;
    }

    // A 卡可选资产：ComfyUI 官方安装器（含 ROCm）+ See-through 插件 + 离线指引。
    // 用户可自己下载，这里只是代下一键备齐；测试端不跑这条链路（体积大、依赖外网）。
    List<string> DownloadAmdExtras(string dir)
    {
        var errs = new List<string>();
        string sub = Path.Combine(dir, "comfyui_setup");
        try { Directory.CreateDirectory(sub); } catch (Exception ex) { errs.Add("建目录失败: " + ex.Message); return errs; }
        SetStatus("下载 ComfyUI 官方安装器（含 AMD ROCm 支持，约 172MB）…");
        string setupDest = Path.Combine(sub, "ComfyUI-Setup.exe");
        bool ok = File.Exists(setupDest) && new FileInfo(setupDest).Length > 100L * 1048576;
        if (!ok)
            ok = DownloadToFile(new string[] {
                    "https://download.comfy.org/windows/nsis/x64",
                    "https://dl.todesktop.com/241130tqe9q3y/windows/nsis/x64" },
                setupDest, 100L * 1048576, delegate(string p) { SetStatus("下载 ComfyUI 安装器 " + p); });
        if (!ok) errs.Add("ComfyUI 安装器下载失败（可自行到 https://www.comfy.org/download 下载）");

        SetStatus("下载 See-through 插件…");
        string zipDest = Path.Combine(sub, "ComfyUI-See-through.zip");
        bool ok2 = File.Exists(zipDest) && new FileInfo(zipDest).Length > 10000;
        if (!ok2)
            ok2 = DownloadToFile(new string[] {
                    "https://ghfast.top/https://github.com/jtydhr88/ComfyUI-See-through/archive/refs/heads/master.zip",
                    "https://github.com/jtydhr88/ComfyUI-See-through/archive/refs/heads/master.zip" },
                zipDest, 10000, null);
        if (!ok2) errs.Add("插件下载失败（可在 ComfyUI-Manager 里搜索 see-through 一键安装）");

        WriteAmdGuide(dir);
        return errs;
    }

    // A 卡：不下载任何资产，只落地一份离线指引（ComfyUI 与插件由用户自装）
    static string WriteAmdGuide(string dir)
    {
        try
        {
            File.WriteAllText(Path.Combine(dir, "A卡拆图指引.txt"), AmdGuide(dir),
                              new UTF8Encoding(false));
            return "已生成 A卡拆图指引.txt（ComfyUI 与插件请自行下载，链接见文件）。";
        }
        catch (Exception ex) { return "指引写入失败: " + ex.Message; }
    }

    static string AmdGuide(string dir)
    {
        return
"木偶工作室 — AMD 显卡拆图指引\r\n" +
"========================================\r\n\r\n" +
"为什么 A 卡要借 ComfyUI？\r\n" +
"  应用内置的 SeeThrough 引擎用 4bit 量化（bitsandbytes），只支持 NVIDIA CUDA。\r\n" +
"  同一套 SeeThrough 模型在 AMD 显卡上可以靠 ComfyUI + ROCm 以 bf16 跑，因此\r\n" +
"  本应用在检测不到 CUDA 时会自动改走「本机 ComfyUI」通道提交拆图任务。\r\n" +
"  除拆图外，本应用全部功能（编辑/骨架/支点/眨眼/导出 PSD·AE·JSON·MP4·GIF、\r\n" +
"  右键基础拆分与 SAM 智能拆分）A 卡都可直接使用，无需 ComfyUI。\r\n\r\n" +
"需要你自己下载的两样东西（本安装器不代下，避免版本漂移与限速失败）\r\n" +
"----------------------------------------\r\n" +
"1) ComfyUI（含 AMD ROCm 支持的官方 Windows 安装器，约 172MB）\r\n" +
"   官方下载：https://www.comfy.org/download\r\n" +
"   驱动：请先更新到最新 AMD Adrenalin 驱动；若 ComfyUI 提示缺运行库，\r\n" +
"         再装 HIP SDK：https://www.amd.com/en/developer/resources/rocm-hub/hip-sdk.html\r\n\r\n" +
"2) See-through 插件（本应用调用的正是它的节点）\r\n" +
"   最简单：ComfyUI 里打开 Manager → 搜索 \"see-through\" → 安装 → 重启\r\n" +
"   手动：把仓库放到 ComfyUI\\custom_nodes\\ 下\r\n" +
"         https://github.com/jtydhr88/ComfyUI-See-through\r\n" +
"   GitHub 直连失败的地区可用镜像下载 zip 后解压：\r\n" +
"         https://ghfast.top/https://github.com/jtydhr88/ComfyUI-See-through/archive/refs/heads/master.zip\r\n\r\n" +
"日常用法（推荐，应用内一键）\r\n" +
"----------------------------------------\r\n" +
"1) 启动 ComfyUI（默认端口 8188，保持开着）；\r\n" +
"2) 打开木偶工作室 → 菜单「拆分图片（SeeThrough）」→ 选插画；\r\n" +
"   应用会自动以 quant_mode=none(bf16) 提交工作流、轮询、把分层结果收进工程；\r\n" +
"3) 首次运行插件会自动从 HuggingFace 拉 SeeThrough 模型，约 12.5GB\r\n" +
"   （layerdiff3d 9.46GB + marigold 3.05GB，是 N 卡 nf4 版 5.31GB 的 2.4 倍）。\r\n" +
"   注意落点在 **C 盘** %USERPROFILE%\\.cache\\huggingface\\hub，不是 ComfyUI\\models！\r\n" +
"   C 盘紧张就给 ComfyUI 进程设环境变量改盘，例如：\r\n" +
"       set HF_HOME=D:\\hfcache\r\n" +
"   国内直连 HuggingFace 失败时同样在服务进程里设：\r\n" +
"       set HF_ENDPOINT=https://hf-mirror.com\r\n" +
"   下载中断不用重来，重跑会自动续传。\r\n" +
"4) 拆完直接在应用里绑骨架、调眨眼、导出。\r\n\r\n" +
"备用用法（ComfyUI 里手动跑）\r\n" +
"----------------------------------------\r\n" +
"在 ComfyUI 导入插件自带工作流跑完，输出目录会得到若干分层 PNG 和一份\r\n" +
"*_layers.json（注意：插件的 SeeThrough_SavePSD 节点并不产出 PSD 文件，\r\n" +
"PSD 是它网页界面上的 \"Download PSD\" 按钮在浏览器里合成的——我们不需要它）。\r\n" +
"回到木偶工作室：菜单「打开工程」→ 直接选那个 ComfyUI 输出目录即可，\r\n" +
"应用认 layers_meta.json，也认 ComfyUI 的 *_layers.json。\r\n\r\n" +
"常见问题\r\n" +
"----------------------------------------\r\n" +
"  · 应用默认 use_lama=false：LaMa 修发丝边缘更好看，但要多下一个 ckpt\r\n" +
"    （首次经 torch.hub 拉，可能失败）。想要更好效果就在 ComfyUI 里手动跑\r\n" +
"    工作流并把 PostProcess 的 use_lama 设为 true，再用「打开工程」导入。\r\n" +
"  · N 卡也想跑全精度（画质更高、更慢更吃显存）：set MUPPET_ENGINE=comfy\r\n" +
"  · 提示「没找到在跑的 ComfyUI」→ ComfyUI 没启动，或端口不是 8188；\r\n" +
"    可用环境变量 COMFY_API 指定，例如 set COMFY_API=http://192.168.1.20:8188\r\n" +
"  · 提示「缺 See-through 插件」→ 按上面第 2 步装插件后重启 ComfyUI\r\n" +
"  · 想强制走某条路线 → set MUPPET_ENGINE=comfy（或 local）\r\n" +
"  · 模型与产物不受更新影响：更新只覆盖程序文件，models\\ 与 seethrough_out\\ 不动\r\n" +
"  · 本应用安装目录：" + dir + "\r\n";
    }

    // ---------------- 全新模式 ----------------
    int DoFresh(string dir, bool withModels, bool noPause, bool withAmd = false)
    {
        try
        {
        string full;
        try { full = Path.GetFullPath(dir).TrimEnd('\\', '/'); }
        catch (Exception ex) { Report(noPause, "路径无效: " + ex.Message); return 2; }
        string root = Path.GetPathRoot(full);
        if (dir.Trim().Length == 0 || full == (root ?? "").TrimEnd('\\', '/')) { Report(noPause, "拒绝安装到盘根目录"); return 2; }
        string winDir = Environment.GetFolderPath(Environment.SpecialFolder.Windows).TrimEnd('\\');
        // 目录本身或其子树都拒绝（StartsWith 带斜杠会漏掉目录本身，曾踩）
        if (string.Equals(full, winDir, StringComparison.OrdinalIgnoreCase) ||
            full.StartsWith(winDir + "\\", StringComparison.OrdinalIgnoreCase))
        { Report(noPause, "拒绝安装到 Windows 目录"); return 2; }
        foreach (string b in new string[] {
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            Environment.GetFolderPath(Environment.SpecialFolder.SystemX86) })
            if (!string.IsNullOrEmpty(b) && string.Equals(b.TrimEnd('\\', '/'), full, StringComparison.OrdinalIgnoreCase))
            { Report(noPause, "拒绝安装到该目录本身: " + b); return 2; }

        SetStatus("正在解压应用…");
        try { Directory.CreateDirectory(full); } catch (Exception ex) { Report(noPause, "无法创建目录: " + ex.Message); return 2; }
        List<string> errors = new List<string>();
        ExtractOverwrite(appZip, full, errors);
        WriteRegistry(full);
        try { MakeShortcuts(full); } catch { }
        bool linkOk = true;
        if (withModels)
        {
            // 下载前先探测镜像链路，不通就别白等
            string linkErr = CheckLinkAvailable();
            if (linkErr != null)
            {
                linkOk = false;
                string m = "无法连接模型下载源，模型未下载。\n\n" + linkErr +
                           "\n\n应用本体已安装完成，可稍后联网时重新运行安装器（选更新/新安装并勾选模型）补装。" +
                           "\n提示：若开着加速器/VPN 请先关闭；下载被 429 限流时红拨宽带或重启路由器刷新公网 IP，再点安装可断点续传。";
                if (!noPause) MessageBox.Show(m, "网络不通", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                Report(noPause, m);
            }
            else
            {
                try { DownloadModels(full); }
                catch (Exception ex) { errors.Add("模型: " + ex.Message); }
            }
        }
        string amdNote = "";
        if (withAmd)
        {
            List<string> aerrs = DownloadAmdExtras(full);
            foreach (string e in aerrs) errors.Add(e);
            amdNote = aerrs.Count == 0
                ? "\nA 卡资产已就位：comfyui_setup\\（ComfyUI 安装器 + See-through 插件 + 指引）"
                : "\nA 卡资产部分失败（见下方失败项，可重跑安装器补下，支持断点续传）";
        }
        string msg = "安装完成: " + full +
            (withModels ? (linkOk ? "\n模型已就位。" : "\n（模型未下载：网络不通）")
                        : "\n（未下载模型，可随时重新运行安装器选择更新/补装。）") + amdNote;
        if (errors.Count > 0) msg += "\n失败 " + errors.Count + " 项: " + errors[0];
        Report(noPause, msg);
        return (errors.Count == 0 && linkOk) ? 0 : 1;
        }
        catch (Exception ex)
        {
            Report(noPause, "安装异常: " + ex.Message);
            return 1;
        }
    }

    // 探测单个 URL：null=可用，否则=错误摘要
    static string ProbeUrl(string url)
    {
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
            req.UserAgent = "Mozilla/5.0";
            req.Timeout = 15000;
            req.ReadWriteTimeout = 15000;
            req.AddRange(0, 0);   // GET+Range（HEAD 在部分镜像行为不一，实测 GET 更稳）
            using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
            {
                if (resp.StatusCode == HttpStatusCode.OK || resp.StatusCode == HttpStatusCode.PartialContent)
                { resp.Close(); return null; }   // 通了
                return "HTTP " + (int)resp.StatusCode;
            }
        }
        catch (WebException wex)
        {
            string kind = wex.Status == WebExceptionStatus.Timeout ? "超时"
                        : wex.Status == WebExceptionStatus.NameResolutionFailure ? "DNS解析失败"
                        : wex.Status == WebExceptionStatus.ConnectFailure ? "无法连接"
                        : (wex.Response != null ? "HTTP " + (int)((HttpWebResponse)wex.Response).StatusCode : wex.Status.ToString());
            return kind;
        }
        catch (Exception ex) { return ex.Message; }
    }

    // 网络诊断：加速器虚拟网卡 / 镜像实测速度 / 429 限流识别。
    // 公网 IP 由路由器持有，程序无法自动"刷新 IP"，只能检测并指引用户处理。
    static string DiagnoseNetwork()
    {
        var sb = new StringBuilder();
        // 1) 加速器/VPN 虚拟网卡与 TUN 特征地址（雷神常见 26.x 网关）
        bool acc = false;
        try
        {
            foreach (NetworkInterface ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (ni.OperationalStatus != OperationalStatus.Up) continue;
                string dl = (ni.Description ?? "").ToLower();
                bool hit = dl.Contains("tap") || dl.Contains("tun") || dl.Contains("leigod")
                        || dl.Contains("biubiu") || dl.Contains("vpn") || dl.Contains("watt")
                        || dl.Contains("加速器") || dl.Contains("netease");
                try
                {
                    foreach (UnicastIPAddressInformation ua in ni.GetIPProperties().UnicastAddresses)
                        if (ua.Address.ToString().StartsWith("26.")) hit = true;
                }
                catch { }
                if (hit)
                {
                    acc = true;
                    sb.AppendLine("[加速器] 检测到虚拟网卡：" + ni.Description);
                }
            }
        }
        catch { }
        if (acc)
            sb.AppendLine("          → 请先关闭加速器（含后台常驻）再下载模型，")
              .AppendLine("            或在加速器设置里把本安装程序加入直连白名单。");
        // 2) 镜像实测（Range 拉 1MB，12 秒封顶）
        string url = HOSTS[0] + "24yearsold/seethroughv0.0.2_layerdiff3d_nf4/resolve/main/unet/diffusion_pytorch_model.safetensors";
        long got = 0; bool rate429 = false; string netErr = null;
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
            req.UserAgent = "Mozilla/5.0";
            req.Timeout = 15000;
            req.AddRange(0, 1048575);
            Stopwatch sw = Stopwatch.StartNew();
            using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
            using (Stream s = resp.GetResponseStream())
            {
                byte[] buf = new byte[65536];
                int n;
                while ((n = s.Read(buf, 0, buf.Length)) > 0)
                {
                    got += n;
                    if (sw.ElapsedMilliseconds > 12000) break;
                }
            }
            sw.Stop();
            double mbps = got / 1048576.0 / Math.Max(0.1, sw.Elapsed.TotalSeconds);
            sb.AppendLine("[速度] 镜像实测 " + mbps.ToString("0.00") + " MB/s"
                + (mbps < 0.5 ? "（过慢：5.3GB 需数小时）" : "（正常）"));
            if (mbps < 0.5 && !acc)
                sb.AppendLine("          → 若开着加速器请先关闭；无加速器仍慢可尝试红拨宽带/重启路由器刷新公网 IP 后重试。");
        }
        catch (WebException wex)
        {
            HttpWebResponse hr = wex.Response as HttpWebResponse;
            if (hr != null && (int)hr.StatusCode == 429)
            {
                rate429 = true;
                sb.AppendLine("[限流] 镜像返回 429：当前公网 IP 请求过多被临时限速。");
            }
            else
                netErr = wex.Status == WebExceptionStatus.Timeout ? "连接超时"
                       : wex.Status == WebExceptionStatus.NameResolutionFailure ? "DNS 解析失败"
                       : wex.Status == WebExceptionStatus.ConnectFailure ? "无法连接镜像"
                       : (hr != null ? "HTTP " + (int)hr.StatusCode : wex.Status.ToString());
        }
        catch (Exception ex) { netErr = ex.Message; }
        if (netErr != null)
            sb.AppendLine("[连接] " + netErr);
        if (rate429)
            sb.AppendLine("          → 红拨宽带或重启路由器可更换公网 IP 解除限流；")
              .AppendLine("            换好后点「安装」即可续传（下载支持断点续传，不会白下）。");
        if (sb.Length == 0) sb.Append("网络正常，可直接下载。");
        return sb.ToString();
    }

    // 下载前探测：任一镜像能取到小文件即算通。返回 null=可用，否则=错误摘要。
    string CheckLinkAvailable()
    {
        // 用两个仓库最小的 model_index.json 做连通性探针
        string[] probes = {
            "24yearsold/seethroughv0.0.2_layerdiff3d_nf4/resolve/main/model_index.json",
            "24yearsold/seethroughv0.0.1_marigold_nf4/resolve/main/model_index.json",
        };
        var fails = new List<string>();
        foreach (string host in HOSTS)
        {
            string err = ProbeUrl(host + probes[0]);
            if (err == null) return null;   // 通了
            fails.Add(new Uri(host).Host + ": " + err);
        }
        return "探测结果：\n  " + string.Join("\n  ", fails.ToArray());
    }

    void WriteRegistry(string dir)
    {
        try
        {
            using (RegistryKey k = Registry.CurrentUser.CreateSubKey(REG_KEY))
                if (k != null) k.SetValue("InstallPath", Path.GetFullPath(dir));
        }
        catch { }
    }

    // ---------------- 模型下载（同旧版） ----------------
    static string[] HOSTS = { "https://hf-mirror.com/", "https://huggingface.co/" };

    void DownloadModels(string dir)
    {
        List<Dict> files = ParseManifest();
        long totalSize = 0;
        foreach (Dict f in files) totalSize += long.Parse(f["size"]);
        string modelRoot = Path.Combine(dir, "models", "24yearsold");
        Directory.CreateDirectory(modelRoot);
        long doneBytes = 0;
        foreach (Dict f in files)
        {
            if (cancelFlag) throw new Exception("已取消");
            long size = long.Parse(f["size"]);
            string dest = ModelDest(dir, f);   // 含 repo 子目录，与应用加载路径一致
            string part = dest + ".part";
            Directory.CreateDirectory(Path.GetDirectoryName(dest));
            if (File.Exists(dest) && new FileInfo(dest).Length == size) { doneBytes += size; continue; }
            long have = File.Exists(part) ? new FileInfo(part).Length : 0;
            if (have > size) { File.Delete(part); have = 0; }
            SetStatus(string.Format("下载模型 {0}（{1}/{2} MB）", Path.GetFileName(f["file"]), have / 1048576, size / 1048576));
            bool ok = false;
            foreach (string host in HOSTS)
            {
                if (cancelFlag) throw new Exception("已取消");
                try { if (DownloadOne(host, f, dest, part, size, ref have, ref doneBytes, totalSize)) { ok = true; break; } }
                catch (Exception ex) { SetStatus("镜像失败换下一个: " + ex.Message.Substring(0, Math.Min(60, ex.Message.Length))); }
            }
            if (!ok) throw new Exception("模型下载失败: " + f["file"]);
        }
    }

    bool DownloadOne(string host, Dict f, string dest, string part, long size, ref long have, ref long doneBytes, long totalSize)
    {
        string url = host + f["repo"] + "/resolve/main/" + f["file"];
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
        req.UserAgent = "Mozilla/5.0";
        req.ReadWriteTimeout = 60000;
        req.Timeout = 30000;
        if (have > 0) req.AddRange(have, size - 1);
        using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
        {
            if (resp.StatusCode == HttpStatusCode.OK && have > 0) { have = 0; if (File.Exists(part)) File.Delete(part); }
            using (Stream net = resp.GetResponseStream())
            using (FileStream fs = new FileStream(part, have > 0 ? FileMode.Append : FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20))
            {
                byte[] buf = new byte[1 << 20];
                int n; long got = 0;
                while ((n = net.Read(buf, 0, buf.Length)) > 0)
                {
                    if (cancelFlag) { fs.Flush(); return false; }
                    fs.Write(buf, 0, n);
                    got += n;
                    ReportProgress(300 + (doneBytes + have + got) * 680 / totalSize, 1000);
                }
            }
        }
        if (new FileInfo(part).Length != size) return false;
        if (File.Exists(dest)) File.Delete(dest);
        File.Move(part, dest);
        doneBytes += size;
        return true;
    }

    // ---------------- manifest 极简解析（同旧版） ----------------
    List<Dict> ParseManifest()
    {
        List<Dict> list = new List<Dict>();
        foreach (string objRaw in SplitObjects(manifest))
        {
            Dict d = new Dict();
            foreach (string kv in SplitKV(objRaw))
            {
                int c = kv.IndexOf(':');
                if (c <= 0) continue;
                string k = kv.Substring(0, c).Trim().Trim('"');
                string v = kv.Substring(c + 1).Trim();
                if (v.StartsWith("\"")) v = v.Trim('"');
                d[k] = v;
            }
            list.Add(d);
        }
        return list;
    }
    static IEnumerable<string> SplitObjects(string json)
    {
        int depth = 0; bool inStr = false; int start = -1;
        for (int i = 0; i < json.Length; i++)
        {
            char c = json[i];
            if (inStr) { if (c == '"' && json[i - 1] != '\\') inStr = false; continue; }
            if (c == '"') inStr = true;
            else if (c == '{') { if (depth == 0) start = i + 1; depth++; }
            else if (c == '}') { depth--; if (depth == 0 && start >= 0) { yield return json.Substring(start, i - start); start = -1; } }
        }
    }
    static IEnumerable<string> SplitKV(string obj)
    {
        int depth = 0; bool inStr = false; int start = 0;
        for (int i = 0; i < obj.Length; i++)
        {
            char c = obj[i];
            if (inStr) { if (c == '"' && obj[i - 1] != '\\') inStr = false; continue; }
            if (c == '"') inStr = true;
            else if (c == '{' || c == '[') depth++;
            else if (c == '}' || c == ']') depth--;
            else if (c == ',' && depth == 0) { yield return obj.Substring(start, i - start); start = i + 1; }
        }
        if (obj.Length > start) yield return obj.Substring(start);
    }
    class Dict
    {
        Dictionary<string, string> m = new Dictionary<string, string>();
        public string this[string k] { get { return m.ContainsKey(k) ? m[k] : ""; } set { m[k] = value; } }
    }

    // ---------------- 快捷方式 ----------------
    void MakeShortcuts(string dir)
    {
        Type t = Type.GetTypeFromProgID("WScript.Shell");
        if (t == null) return;
        dynamic sh = Activator.CreateInstance(t);
        string exe = Path.Combine(dir, "木偶工作室.exe");
        if (!File.Exists(exe)) return;
        string startMenu = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
        string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
        foreach (string folder in new string[] { startMenu, desktop })
        {
            try
            {
                dynamic lnk = sh.CreateShortcut(Path.Combine(folder, "木偶工作室.lnk"));
                lnk.TargetPath = exe;
                lnk.WorkingDirectory = dir;
                lnk.IconLocation = Path.Combine(dir, "anon_sewing_icon.ico");
                lnk.Description = "木偶工作室 —— SeeThrough 分层骨架动画工作台";
                lnk.Save();
            }
            catch { }
        }
    }

    // ---------------- UI 线程工具 ----------------
    void InvokeOnUI(Action a) { try { if (win != null) win.BeginInvoke(a); } catch { } }
    void SetStatus(string s) { InvokeOnUI(delegate { if (status != null) status.Text = s; }); }
    void ReportProgress(long v, long max)
    {
        int px = (int)Math.Min(1000, v * 1000 / Math.Max(1, max));
        InvokeOnUI(delegate { if (bar != null) bar.Value = Math.Max(bar.Value, px); });
    }
    void Report(bool noPause, string msg)
    {
        if (noPause)
        {
            string rp = Environment.GetEnvironmentVariable("MUPPET_SETUP_REPORT");
            if (!string.IsNullOrEmpty(rp)) { try { File.WriteAllText(rp, msg, new UTF8Encoding(false)); } catch { } }
            Console.WriteLine(msg);
            return;
        }
        SetStatus(msg.Split('\n')[0]);
        MessageBox.Show(win, msg, "木偶工作室 安装", MessageBoxButtons.OK, MessageBoxIcon.Information);
    }

    // ---------------- GUI ----------------
    void ShowGui(bool relaunched)
    {
        detected = DetectInstall();

        win = new Form();
        win.Text = "木偶工作室 安装向导";
        win.ClientSize = new System.Drawing.Size(760, 400);
        win.FormBorderStyle = FormBorderStyle.FixedDialog;
        win.MaximizeBox = false; win.MinimizeBox = true;   // 允许最小化，下载时可缩到任务栏后台跑
        win.StartPosition = FormStartPosition.CenterScreen;

        // ===== 左侧竖版配图（ArcGIS 安装器风格）=====
        // banner 作为嵌入资源打进 exe，离线安装也能显示
        PictureBox banner = new PictureBox();
        banner.SizeMode = PictureBoxSizeMode.StretchImage;
        banner.SetBounds(0, 0, 220, 400);
        try
        {
            using (Stream bs = Assembly.GetExecutingAssembly()
                       .GetManifestResourceStream("installer_banner.png"))
            {
                if (bs != null)
                {
                    using (MemoryStream ms = new MemoryStream())
                    {
                        bs.CopyTo(ms);
                        ms.Position = 0;
                        banner.Image = Image.FromStream(ms);
                    }
                }
            }
        }
        catch { }
        win.Controls.Add(banner);

        // 右侧表单区（左侧 220px 给配图）
        const int LX = 240;
        const int LW = 500;

        radioUpdate = new RadioButton();
        radioUpdate.Text = "更新版本";
        radioUpdate.SetBounds(LX, 20, 100, 24);
        radioUpdate.Enabled = !string.IsNullOrEmpty(detected);
        win.Controls.Add(radioUpdate);

        detectLbl = new Label();
        detectLbl.Text = string.IsNullOrEmpty(detected) ? "未检测到已安装版本" : ("检测到旧版本: " + detected);
        detectLbl.SetBounds(LX + 100, 22, LW - 100, 20);
        win.Controls.Add(detectLbl);

        radioFresh = new RadioButton();
        radioFresh.Text = "新安装";
        radioFresh.SetBounds(LX, 52, 100, 24);
        radioFresh.Checked = string.IsNullOrEmpty(detected);
        win.Controls.Add(radioFresh);

        dirBox = new TextBox();
        dirBox.SetBounds(LX + 100, 52, 300, 24);
        dirBox.Text = string.IsNullOrEmpty(detected)
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "木偶工作室")
            : detected;
        win.Controls.Add(dirBox);

        browseBtn = new Button();
        browseBtn.Text = "浏览…";
        browseBtn.SetBounds(LX + 408, 51, 76, 26);
        browseBtn.Click += delegate { Browse(); };
        win.Controls.Add(browseBtn);

        modelChk = new CheckBox();
        modelChk.Text = "下载模型权重（nf4 量化，约 5.3 GB，需联网，支持断点续传）";
        modelChk.SetBounds(LX + 2, 86, LW - 4, 24);
        win.Controls.Add(modelChk);

        Label l3 = new Label();
        l3.Text = "更新只覆盖程序文件，模型与你的拆分产物（seethrough_out\\）保持不变；\r\n" +
                 "新安装会创建开始菜单与桌面快捷方式，并把安装路径记录到注册表供下次更新检测。";
        l3.SetBounds(LX + 2, 166, LW - 4, 40);
        win.Controls.Add(l3);

        // 显卡路线：安装前检测，A 卡自动勾选 ComfyUI(ROCm) 路线（N 卡不受影响）
        gpuV = GpuVendor(out gpuN);
        gpuLbl = new Label();
        gpuLbl.SetBounds(LX + 2, 112, LW - 4, 20);
        gpuLbl.Text = gpuV == "nvidia" ? ("检测到显卡：" + gpuN + "（N 卡，应用内可直接拆图）")
                    : gpuV == "amd" ? ("检测到显卡：" + gpuN + "（A 卡，建议走 ComfyUI + ROCm 拆图）")
                    : ("检测到显卡：" + gpuN);
        win.Controls.Add(gpuLbl);

        amdChk = new CheckBox();
        amdChk.SetBounds(LX + 2, 136, LW - 4, 24);
        amdChk.Text = "A 卡拆图支持：代下 ComfyUI 安装器(172MB) + See-through 插件 + 中文指引（也可自行下载，见指引）";
        if (gpuV == "amd") amdChk.Checked = true;
        win.Controls.Add(amdChk);

        bar = new ProgressBar();
        bar.SetBounds(LX + 2, 222, LW - 4, 20);
        bar.Maximum = 1000;
        win.Controls.Add(bar);

        status = new Label();
        status.Text = "就绪";
        status.SetBounds(LX + 2, 246, LW - 4, 20);
        win.Controls.Add(status);

        installBtn = new Button();
        installBtn.Text = "安 装";
        installBtn.SetBounds(LX + 20, 300, 110, 34);
        installBtn.Click += delegate { StartInstall(relaunched); };
        win.Controls.Add(installBtn);

        Button diagBtn = new Button();
        diagBtn.Text = "网络诊断";
        diagBtn.SetBounds(LX + 140, 300, 110, 34);
        diagBtn.Click += delegate
        {
            if (running) return;
            diagBtn.Enabled = false;
            status.Text = "正在诊断网络（约 15 秒）…";
            Thread t = new Thread(new ThreadStart(delegate
            {
                string rep = DiagnoseNetwork();
                InvokeOnUI(delegate
                {
                    status.Text = "诊断完成";
                    diagBtn.Enabled = true;
                    MessageBox.Show(win, rep, "网络诊断", MessageBoxButtons.OK, MessageBoxIcon.Information);
                });
            }));
            t.IsBackground = true;
            t.Start();
        };
        win.Controls.Add(diagBtn);

        Button quit = new Button();
        quit.Text = "退出";
        quit.SetBounds(LX + 370, 300, 100, 34);
        quit.Click += delegate { if (!running) win.Close(); else cancelFlag = true; };
        win.Controls.Add(quit);
        repairBtn = new Button();
        repairBtn.Text = "修复模型";
        repairBtn.SetBounds(LX + 260, 300, 100, 34);
        repairBtn.Click += delegate
        {
            if (running) return;
            string d = dirBox.Text.Trim();
            if (d.Length == 0) { MessageBox.Show(win, "请填写安装目录", "提示", MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }
            if (!Directory.Exists(d)) { MessageBox.Show(win, "目录不存在: " + d, "提示", MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }
            if (AppRunning()) { MessageBox.Show(win, "木偶工作室正在运行，请先关闭再修复", "提示", MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }
            running = true; installBtn.Enabled = false; repairBtn.Enabled = false;
            browseBtn.Enabled = false; diagBtn.Enabled = false; quit.Enabled = false;
            cancelFlag = false;
            Thread rt = new Thread(new ThreadStart(delegate
            {
                string rep;
                try
                {
                    var sb = new StringBuilder();
                    // 步骤 1：程序文件更新到最新（本安装器内置的版本即最新版）
                    // 复用 ExtractOverwrite：它就是"覆盖程序文件、绝不动 models/ 与产物"
                    SetStatus("正在更新程序文件到最新版…");
                    List<string> uerr = new List<string>();
                    long written = ExtractOverwrite(appZip, d, uerr);
                    sb.AppendLine("程序文件已更新到最新版（覆盖 " + written + " 个文件）。");
                    if (uerr.Count > 0) sb.AppendLine("其中有 " + uerr.Count + " 个文件未能写入。");
                    WriteRegistry(d);
                    try { MakeShortcuts(d); } catch { }
                    // 步骤 2：整理模型目录（平铺文件归位 + 清 .part）
                    SetStatus("正在扫描并整理模型目录…");
                    int moved = 0;
                    List<string> bad = RepairModels(d, out moved);
                    sb.AppendLine("模型目录整理：归位 " + moved + " 个文件。");
                    // 步骤 3：只补缺失/损坏的文件
                    if (bad.Count == 0)
                    {
                        sb.AppendLine("模型已完整，无需下载。");
                    }
                    else
                    {
                        sb.AppendLine("缺失/损坏 " + bad.Count + " 个文件，开始补下…");
                        string linkErr = CheckLinkAvailable();
                        if (linkErr != null)
                        {
                            sb.AppendLine("无法连接下载源，未下载。\n" + linkErr);
                        }
                        else
                        {
                            SetStatus("正在补下缺失的 " + bad.Count + " 个文件…");
                            DownloadOnlyMissing(d, bad);
                            List<string> still = FindBadModels(d);
                            sb.AppendLine(still.Count == 0
                                ? "缺失文件已全部补齐，修复完成。"
                                : ("仍有 " + still.Count + " 个文件未完成，可再次点修复续传。"));
                        }
                    }
                    rep = sb.ToString();
                }
                catch (Exception ex) { rep = "修复失败: " + ex.Message; }
                running = false;
                InvokeOnUI(delegate
                {
                    installBtn.Enabled = true; repairBtn.Enabled = true;
                    browseBtn.Enabled = !radioUpdate.Checked;
                    diagBtn.Enabled = true; quit.Enabled = true;
                    SetStatus("就绪");
                    MessageBox.Show(win, rep, "修复完成", MessageBoxButtons.OK, MessageBoxIcon.Information);
                });
            }));
            rt.IsBackground = true;
            rt.Start();
        };
        win.Controls.Add(repairBtn);

        radioUpdate.CheckedChanged += delegate { SyncModeUI(); };
        radioFresh.CheckedChanged += delegate { SyncModeUI(); };
        SyncModeUI();

        win.FormClosing += delegate(object s, FormClosingEventArgs e)
        {
            if (running) { cancelFlag = true; e.Cancel = true; SetStatus("正在取消，请稍候…"); }
            else { try { File.Delete(appZip); } catch { } }
        };
        Application.Run(win);
    }

    void SyncModeUI()
    {
        bool upd = radioUpdate.Checked;
        dirBox.ReadOnly = upd;
        browseBtn.Enabled = !upd;
        if (upd) dirBox.Text = detected;
        string target = dirBox.Text.Trim();
        bool hasModels = HasModels(target);
        if (hasModels)
        {
            List<string> badM = FindBadModels(target);
            if (badM.Count == 0)
            {
                modelChk.Checked = false;
                modelChk.Enabled = false;
                modelChk.Text = "模型完整（5.3 GB），无需下载";
            }
            else
            {
                modelChk.Checked = true;
                modelChk.Enabled = false;
                modelChk.Text = "检测到 " + badM.Count + " 个模型文件不完整，本次更新将自动修复（重下坏文件，需联网）";
            }
        }
        else
        {
            modelChk.Enabled = true;
            modelChk.Checked = true;
            modelChk.Enabled = gpuV != "amd";
            modelChk.Checked = gpuV != "amd";
            modelChk.Text = gpuV == "amd"
                ? "本地引擎模型（nf4 5.3GB）— A 卡不可用，已跳过"
                : (upd
                    ? "未检测到模型，本次更新顺带下载（约 5.3 GB，支持断点续传）"
                    : "下载模型权重（nf4 量化，约 5.3 GB，需联网，支持断点续传）");
        }
        // A 卡才需要 ComfyUI 路线；N 卡用户禁用该项（不影响其原有流程）
        if (gpuV == "nvidia")
        {
            amdChk.Checked = false;
            amdChk.Enabled = false;
            amdChk.Text = "A 卡拆图支持：当前为 N 卡，应用内可直接拆图，无需下载";
        }
        else if (gpuV == "amd")
        {
            amdChk.Enabled = true;
            amdChk.Checked = true;
            amdChk.Text = "A 卡拆图支持（已检测到 A 卡，自动勾选）：代下 ComfyUI 安装器 + 插件 + 指引";
        }
        else
        {
            amdChk.Enabled = true;
            amdChk.Text = "A 卡拆图支持（若你的显卡是 AMD 或本机无 N 卡，可勾选代下；也可自行下载）";
        }
    }

    void Browse()
    {
        using (FolderBrowserDialog fd = new FolderBrowserDialog())
        {
            fd.Description = "选择安装目录";
            try { fd.SelectedPath = dirBox.Text; } catch { }
            if (fd.ShowDialog(win) == DialogResult.OK)
            {
                dirBox.Text = fd.SelectedPath;
                radioFresh.Checked = true;
                SyncModeUI();
            }
        }
    }

    void StartInstall(bool relaunched)
    {
        if (running) return;
        string dir = dirBox.Text.Trim();
        if (dir.Length == 0) { MessageBox.Show(win, "请填写安装目录", "提示", MessageBoxButtons.OK, MessageBoxIcon.Warning); return; }

        // 自覆盖保护：Setup 位于目标目录内 -> 复制到 temp 重启自己
        string self = Assembly.GetExecutingAssembly().Location;
        if (!relaunched && Path.GetFullPath(dir).TrimEnd('\\').StartsWith(Path.GetDirectoryName(self), StringComparison.OrdinalIgnoreCase))
        {
            try
            {
                string tmp = Path.Combine(Path.GetTempPath(), "muppet_setup_" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".exe");
                File.Copy(self, tmp, true);
                Process.Start(tmp, "--relaunched");
                win.Close();
                return;
            }
            catch (Exception ex)
            {
                MessageBox.Show(win, "无法安全重启安装器: " + ex.Message, "错误", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
        }

        bool upd = radioUpdate.Checked;
        bool withModels = modelChk.Checked && modelChk.Enabled;
        bool withAmd = amdChk.Checked && amdChk.Enabled;
        running = true; cancelFlag = false;
        installBtn.Enabled = false; browseBtn.Enabled = false;
        Thread t = new Thread(new ThreadStart(delegate
        {
            int code;
            if (upd) code = DoUpdate(dir, withModels, false, withAmd);
            else code = DoFresh(dir, withModels, false, withAmd);
            if (code != 0 && !cancelFlag)
                InvokeOnUI(delegate { status.Text = "未完成 (code " + code + ")"; });
            running = false;
            InvokeOnUI(delegate
            {
                installBtn.Enabled = true; browseBtn.Enabled = true; installBtn.Text = "继 续";
            });
        }));
        t.IsBackground = true;
        t.Start();
    }
}
