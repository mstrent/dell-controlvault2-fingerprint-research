# winbio-probe.ps1 - call Windows' biometric API (WinBio) one step at a time,
# 5 s apart and time-stamped, so a host-side usbmon capture shows what each
# call sends to the fingerprint reader. Public Windows API only.
#
# In the Windows VM, with Settings closed, from the CD drive (D: here):
#   powershell -ExecutionPolicy Bypass -File D:\winbio-probe.ps1
#   powershell -ExecutionPolicy Bypass -File D:\winbio-probe.ps1 -DeleteAll
# -DeleteAll also deletes ALL of this user's Windows Hello fingerprints in one
# call (any finger), to see whether that takes a bulk path. Re-enroll after.
param([switch]$DeleteAll)

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class WinBio {
    const uint TYPE_FINGERPRINT = 0x8, POOL_SYSTEM = 1, FLAG_DEFAULT = 0, ID_TYPE_SID = 3;
    const byte SUBTYPE_ANY = 0xFF;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct UNIT_SCHEMA {
        public uint UnitId, PoolType, BiometricFactor, SensorSubType, Capabilities;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string DeviceInstanceId;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Description;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Manufacturer;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string Model;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string SerialNumber;
        public uint FwMajor, FwMinor;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STORAGE_SCHEMA {
        public uint BiometricFactor;
        public Guid DatabaseId, DataFormat;
        public uint Attributes;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string FilePath;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string ConnectionString;
    }

    // WINBIO_IDENTITY with the AccountSid member of the union (76 bytes)
    [StructLayout(LayoutKind.Sequential)]
    struct IDENTITY {
        public uint Type, SidSize;
        [MarshalAs(UnmanagedType.ByValArray, SizeConst = 68)] public byte[] Sid;
    }

    [DllImport("winbio.dll")] static extern int WinBioEnumBiometricUnits(uint factor, out IntPtr units, out UIntPtr count);
    [DllImport("winbio.dll")] static extern int WinBioEnumDatabases(uint factor, out IntPtr dbs, out UIntPtr count);
    [DllImport("winbio.dll")] static extern int WinBioOpenSession(uint factor, uint pool, uint flags, IntPtr unitArray,
                                                                  UIntPtr unitCount, IntPtr databaseId, out uint session);
    [DllImport("winbio.dll")] static extern int WinBioEnumEnrollments(uint session, uint unitId, ref IDENTITY identity,
                                                                      out IntPtr subFactors, out UIntPtr count);
    [DllImport("winbio.dll")] static extern int WinBioDeleteTemplate(uint session, uint unitId, ref IDENTITY identity, byte subFactor);
    [DllImport("winbio.dll")] static extern int WinBioCloseSession(uint session);
    [DllImport("winbio.dll")] static extern int WinBioFree(IntPtr address);

    static T[] Read<T>(IntPtr p, ulong n) {
        int size = Marshal.SizeOf(typeof(T));
        var r = new T[n];
        for (ulong i = 0; i < n; i++)
            r[i] = (T)Marshal.PtrToStructure(IntPtr.Add(p, (int)i * size), typeof(T));
        return r;
    }

    static IDENTITY Identity(byte[] sid) {
        var id = new IDENTITY { Type = ID_TYPE_SID, SidSize = (uint)sid.Length, Sid = new byte[68] };
        Array.Copy(sid, id.Sid, sid.Length);
        return id;
    }

    public static uint[] UnitIds = new uint[0];

    public static string Units() {
        IntPtr p; UIntPtr n;
        int hr = WinBioEnumBiometricUnits(TYPE_FINGERPRINT, out p, out n);
        if (hr != 0) return String.Format("WinBioEnumBiometricUnits: 0x{0:x8}", hr);
        var units = Read<UNIT_SCHEMA>(p, n.ToUInt64());
        WinBioFree(p);
        var sb = new StringBuilder();
        UnitIds = new uint[units.Length];
        for (int i = 0; i < units.Length; i++) {
            var u = units[i];
            UnitIds[i] = u.UnitId;
            sb.AppendFormat("unit {0}: {1} / {2} / {3}, pool {4}, subtype 0x{5:x}, capabilities 0x{6:x}, fw {7}.{8}\n    {9}\n",
                u.UnitId, u.Description, u.Manufacturer, u.Model, u.PoolType, u.SensorSubType,
                u.Capabilities, u.FwMajor, u.FwMinor, u.DeviceInstanceId);
        }
        return sb.ToString();
    }

    public static string Databases() {
        IntPtr p; UIntPtr n;
        int hr = WinBioEnumDatabases(TYPE_FINGERPRINT, out p, out n);
        if (hr != 0) return String.Format("WinBioEnumDatabases: 0x{0:x8}", hr);
        var dbs = Read<STORAGE_SCHEMA>(p, n.ToUInt64());
        WinBioFree(p);
        var sb = new StringBuilder();
        foreach (var d in dbs)
            sb.AppendFormat("database {0}, format {1}, attributes 0x{2:x8}\n    file '{3}', connection '{4}'\n",
                d.DatabaseId, d.DataFormat, d.Attributes, d.FilePath, d.ConnectionString);
        return sb.ToString();
    }

    public static string Open(out uint session) {
        int hr = WinBioOpenSession(TYPE_FINGERPRINT, POOL_SYSTEM, FLAG_DEFAULT, IntPtr.Zero, UIntPtr.Zero,
                                   IntPtr.Zero, out session);
        return String.Format("WinBioOpenSession: 0x{0:x8}, session {1}", hr, session);
    }

    public static string Enrollments(uint session, uint unit, byte[] sid) {
        var id = Identity(sid);
        IntPtr p; UIntPtr n;
        int hr = WinBioEnumEnrollments(session, unit, ref id, out p, out n);
        if (hr != 0) return String.Format("WinBioEnumEnrollments(unit {0}): 0x{1:x8}", unit, hr);
        ulong count = n.ToUInt64();
        var sb = new StringBuilder(String.Format("WinBioEnumEnrollments(unit {0}): {1} finger(s):", unit, count));
        for (ulong i = 0; i < count; i++)
            sb.AppendFormat(" {0}", Marshal.ReadByte(p, (int)i));
        WinBioFree(p);
        return sb.ToString();
    }

    public static string DeleteAll(uint session, uint unit, byte[] sid) {
        var id = Identity(sid);
        int hr = WinBioDeleteTemplate(session, unit, ref id, SUBTYPE_ANY);
        return String.Format("WinBioDeleteTemplate(unit {0}, any finger): 0x{1:x8}", unit, hr);
    }

    public static string Close(uint session) {
        return String.Format("WinBioCloseSession: 0x{0:x8}", WinBioCloseSession(session));
    }
}
'@

function Step($what) {
    Start-Sleep -Seconds 5
    Write-Host ("`n=== {0} {1}" -f (Get-Date -Format 'HH:mm:ss.fff'), $what) -ForegroundColor Cyan
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$sid = New-Object byte[] $user.BinaryLength
$user.GetBinaryForm($sid, 0)
Write-Host ("Started {0}; user {1}" -f (Get-Date -Format 'HH:mm:ss.fff'), $user.Value)

Step 'WinBioEnumBiometricUnits'
Write-Host ([WinBio]::Units())
Step 'WinBioEnumDatabases'
Write-Host ([WinBio]::Databases())
Step 'WinBioOpenSession (system pool)'
$session = [uint32]0
Write-Host ([WinBio]::Open([ref]$session))
foreach ($unit in [WinBio]::UnitIds) {
    Step "WinBioEnumEnrollments, unit $unit, this user"
    Write-Host ([WinBio]::Enrollments($session, $unit, $sid))
    if ($DeleteAll) {
        Step "WinBioDeleteTemplate, unit $unit, this user, ANY finger"
        Write-Host ([WinBio]::DeleteAll($session, $unit, $sid))
        Step "WinBioEnumEnrollments again, unit $unit"
        Write-Host ([WinBio]::Enrollments($session, $unit, $sid))
    }
}
Step 'WinBioCloseSession'
Write-Host ([WinBio]::Close($session))
Write-Host ("`nDone {0}" -f (Get-Date -Format 'HH:mm:ss.fff'))
