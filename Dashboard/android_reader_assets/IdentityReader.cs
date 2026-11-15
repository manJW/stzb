using System;
using System.Collections;
using System.Reflection;
using System.Globalization;

namespace STLab.IdentityReader
{
    // Read only public role identifiers from already loaded client tables.
    // No network requests, orders, hooks, credentials or table writes.
    public static class Entry
    {
        const BindingFlags Flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance;
        static Type Find(string name) {
            Type found = null;
            foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies()) {
                if (!a.GetName().Name.StartsWith("Game.")) continue;
                Type t = a.GetType(name, false);
                if (t == null) continue;
                if (found != null && found != t) throw new InvalidOperationException("Ambiguous identity type");
                found = t;
            }
            if (found == null) throw new InvalidOperationException("Identity table not loaded: " + name);
            return found;
        }
        static object Field(Type t, object obj, string name) {
            FieldInfo f = t.GetField(name, Flags);
            if (f == null) throw new MissingFieldException(t.FullName, name);
            return f.GetValue(obj);
        }
        static int Int(object obj, string name) { return Convert.ToInt32(Field(obj.GetType(), obj, name), CultureInfo.InvariantCulture); }
        static string Text(object obj, string name) { return Convert.ToString(Field(obj.GetType(), obj, name), CultureInfo.InvariantCulture); }
        static IEnumerable Rows(string name) {
            Type x = Find(name).GetNestedType("X", Flags);
            if (x == null) throw new InvalidOperationException("Identity cache type unavailable");
            object cache = Field(x, null, "sCacheInstance");
            if (cache == null) throw new InvalidOperationException("Identity cache not loaded");
            IDictionary rows = Field(cache.GetType(), cache, "mDataDict") as IDictionary;
            if (rows == null) throw new InvalidOperationException("Identity cache format changed");
            return rows.Values;
        }
        static string Quote(string value) {
            return "\"" + (value ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t") + "\"";
        }
        public static string Identify() { try { return IdentifyCore(); } catch (Exception error) { return "{\"error\":" + Quote(error.Message) + "}"; } }
        static string IdentifyCore() {
            Type net = Find("Tenth.Network.NetManager");
            object instance = Field(net, null, "sInstance");
            object manager = Field(net, instance, "mCmdMgr");
            if (manager == null || Int(manager, "_networkState") != 1) throw new InvalidOperationException("Role is not logged in");
            int current = Int(manager, "_userId"), server = Int(manager, "_serverId");
            if (current <= 0) throw new InvalidOperationException("Role is not logged in");
            object own = null;
            foreach (object row in Rows("Tenth.Data.Tb_user")) {
                if (Int(row, "userid") != current && Int(row, "login_server_userid") != current) continue;
                if (own != null) throw new InvalidOperationException("Multiple matching roles");
                own = row;
            }
            if (own == null) throw new InvalidOperationException("Current role row not loaded");
            int uid = Int(own, "userid"), role = Int(own, "userid"), season = 0;
            int loginMatches = 0; foreach (object row in Rows("Tenth.Data.Tb_user_login")) { if (Int(row, "userid") == uid) loginMatches++; }
            if (loginMatches != 1) throw new InvalidOperationException("Current login row mismatch");
            foreach (object row in Rows("Tenth.Data.Tb_season_user")) {
                if (Int(row, "user_id") != uid) continue;
                if (season != 0) throw new InvalidOperationException("Multiple matching seasons");
                season = Int(row, "season");
            }
            if (uid <= 0 || role <= 0 || server <= 0 || season <= 0) throw new InvalidOperationException("Incomplete role scope");
            if (Int(manager, "_networkState") != 1 || Int(manager, "_userId") != current || Int(manager, "_serverId") != server)
                throw new InvalidOperationException("Role changed during inspection");
            return "{\"user_id\":" + Quote(uid.ToString()) + ",\"account_role_id\":" + Quote(role.ToString())
                + ",\"account_name\":" + Quote(Text(own, "name")) + ",\"union_id\":" + Quote(Int(own, "union_id").ToString())
                + ",\"union_name\":" + Quote(Text(own, "union_name")) + ",\"server_id\":" + server + ",\"season_id\":" + season
                + ",\"battle_role_id\":" + Quote(Text(own, "role_id")) + ",\"force_type\":" + Int(own, "force")
                + ",\"network_user_id\":" + Quote(current.ToString()) + ",\"method\":\"current_client_tables\",\"game_requests_sent\":0}";
        }
    }
}
