using System;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Drawing;
using System.Windows.Forms;
using System.Threading.Tasks;

internal static class DesktopInstaller {
    const string Title="率土战场客户端";
    [STAThread] static int Main(string[] args) {
        try {
            if(!Environment.Is64BitOperatingSystem)throw new InvalidOperationException("需要 64 位 Windows 10 或 Windows 11。");
            if(args.Length==2 && args[0]=="--extract") { Install(Path.GetFullPath(args[1]));return 0; }
            if(args.Length!=0)throw new ArgumentException("无法识别安装参数。");
            Application.EnableVisualStyles();
            using(var form=new Form()) {
                form.Text=Title;form.ClientSize=new Size(540,290);form.FormBorderStyle=FormBorderStyle.FixedDialog;form.MaximizeBox=false;form.StartPosition=FormStartPosition.CenterScreen;
                form.BackColor=Color.FromArgb(247,249,252);form.Font=new Font("Microsoft YaHei UI",9F);
                var heading=new Label{Text="安装率土战场",Font=new Font("Microsoft YaHei UI",19F,FontStyle.Bold),AutoSize=true,Location=new Point(28,25)};
                var note=new Label{Text="游戏已登录后自动识别角色、区服和赛季。\n采集与档案保存在本机。",AutoSize=true,Location=new Point(31,78),ForeColor=Color.FromArgb(100,113,133)};
                string defaultRoot=Directory.Exists(@"F:\")?@"F:\STLab\率土战场":Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"STZB-Battlefield");
                var folder=new TextBox{Text=defaultRoot,Location=new Point(31,136),Size=new Size(389,27)};
                var browse=new Button{Text="选择目录",Location=new Point(430,134),Size=new Size(80,29)};
                browse.Click+=(s,e)=>{using(var dialog=new FolderBrowserDialog()){dialog.Description="选择客户端安装目录";dialog.SelectedPath=folder.Text;if(dialog.ShowDialog(form)==DialogResult.OK)folder.Text=dialog.SelectedPath;}};
                var status=new Label{Text="保留已有档案，更新时不删除历史记录。",Location=new Point(31,176),Size=new Size(480,36),ForeColor=note.ForeColor};
                var install=new Button{Text="安装并打开",Location=new Point(370,231),Size=new Size(140,35),BackColor=Color.FromArgb(49,91,202),ForeColor=Color.White,FlatStyle=FlatStyle.Flat};
                bool working=false;
                form.FormClosing+=(s,e)=>{if(working)e.Cancel=true;};
                install.Click+=async(s,e)=>{
                    working=true;install.Enabled=false;browse.Enabled=false;folder.Enabled=false;status.Text="正在校验并安装，请稍候…";
                    try {
                        string root=Path.GetFullPath(folder.Text.Trim());
                        string program=await Task.Run(()=>Install(root));
                        string exe=Path.Combine(program,@"Client\率土战场.exe");
                        Shortcut(exe,program);
                        Process.Start(new ProcessStartInfo(exe){WorkingDirectory=Path.GetDirectoryName(exe),UseShellExecute=true});
                        working=false;form.Close();
                    }catch(Exception error){status.Text="安装未完成："+error.Message;install.Enabled=true;browse.Enabled=true;folder.Enabled=true;working=false;}
                };
                form.Controls.AddRange(new Control[]{heading,note,folder,browse,status,install});Application.Run(form);
            }
            return 0;
        } catch(Exception error){if(args.Length==0)MessageBox.Show(error.Message,Title,MessageBoxButtons.OK,MessageBoxIcon.Error);else Console.Error.WriteLine(error.Message);return 1;}
    }
    static string Install(string root) {
        root=Path.GetFullPath(root);Directory.CreateDirectory(root);
        using(Stream stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("desktop.zip"))
        using(SHA256 sha=SHA256.Create()) {
            string version=BitConverter.ToString(sha.ComputeHash(stream)).Replace("-","").Substring(0,16);
            string program=Path.Combine(root,"app-"+version);
            if(File.Exists(Path.Combine(program,"installed.complete")))return program;
            string staging=Path.Combine(root,"setup-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(staging);stream.Position=0;
            using(var zip=new ZipArchive(stream,ZipArchiveMode.Read))foreach(var entry in zip.Entries) {
                string target=Path.GetFullPath(Path.Combine(staging,entry.FullName.Replace('/',Path.DirectorySeparatorChar)));
                if(!target.StartsWith(staging+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("安装文件路径异常");
                if(entry.FullName.EndsWith("/")){Directory.CreateDirectory(target);continue;}
                Directory.CreateDirectory(Path.GetDirectoryName(target));entry.ExtractToFile(target,false);
            }
            Verify(staging);
            File.WriteAllText(Path.Combine(staging,"installed.complete"),version);
            Directory.Move(staging,program);
            Directory.CreateDirectory(Path.Combine(root,"Workspace"));
            return program;
        }
    }
    static void Verify(string root) {
        foreach(string line in File.ReadAllLines(Path.Combine(root,"files.sha256"))) {
            if(line.Length<67)continue;
            string name=line.Substring(66);string target=Path.GetFullPath(Path.Combine(root,name.Replace('/',Path.DirectorySeparatorChar)));
            if(!target.StartsWith(root+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("校验路径异常");
            using(var file=File.OpenRead(target))using(var sha=SHA256.Create())if(BitConverter.ToString(sha.ComputeHash(file)).Replace("-","").ToLowerInvariant()!=line.Substring(0,64))throw new InvalidDataException("文件校验失败："+name);
        }
    }
    static void Shortcut(string exe,string program) {
        string file=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),Title+".lnk");
        if(File.Exists(file)&&!File.Exists(file+".previous"))File.Copy(file,file+".previous");
        object shell=Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
        object link=shell.GetType().InvokeMember("CreateShortcut",BindingFlags.InvokeMethod,null,shell,new object[]{file});
        foreach(var pair in new[]{new[]{"TargetPath",exe},new[]{"WorkingDirectory",Path.GetDirectoryName(exe)},new[]{"IconLocation",Path.Combine(program,@"Client\resources\app\icon.ico")}})
            link.GetType().InvokeMember(pair[0],BindingFlags.SetProperty,null,link,new object[]{pair[1]});
        link.GetType().InvokeMember("Save",BindingFlags.InvokeMethod,null,link,null);
    }
}
