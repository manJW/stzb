using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using System.Text;

namespace STLab.MapSelectionReader
{
    // Observe existing map selection events only. No input, game orders or network requests.
    public static class Entry
    {
        static readonly object Gate = new object();
        const BindingFlags Flags = BindingFlags.Static | BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        static object events;
        static EventInfo selectedEvent, cancelledEvent;
        static Action<int, object> selectedHandler, cancelledHandler;
        static int userA, userB, server, army, group;
        static long sequence;
        static long lastSequence;
        static int lastArmy, lastGroup;
        static double lastSelectedAt;
        static double selectedAt;
        static string readerId = "", kind = "";

        static Type Find(string name) {
            Type found = null;
            foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies()) {
                if (!assembly.GetName().Name.StartsWith("Game.")) continue;
                Type type = assembly.GetType(name, false);
                if (type == null) continue;
                if (found != null && found != type) throw new InvalidOperationException("Ambiguous client type");
                found = type;
            }
            if (found == null) throw new InvalidOperationException("Client type not loaded: " + name);
            return found;
        }
        static object Field(Type type, object instance, string name) {
            FieldInfo field = type.GetField(name, Flags);
            if (field == null) throw new MissingFieldException(type.FullName, name);
            return field.GetValue(instance);
        }
        static int Int(object value, string name) { return Convert.ToInt32(Field(value.GetType(), value, name)); }
        static void Verify() {
            Type net = Find("Tenth.Network.NetManager");
            object instance = Field(net, null, "sInstance");
            if (instance == null) throw new InvalidOperationException("Role not logged in");
            object manager = Field(net, instance, "mCmdMgr");
            if (manager == null || Int(manager, "_networkState") != 1
                || (Int(manager, "_userId") != userA && Int(manager, "_userId") != userB)
                || Int(manager, "_serverId") != server)
                throw new InvalidOperationException("Role/server binding changed");
        }
        static double Now() { return (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds; }
        static object InheritedField(object instance, string name) {
            for (Type type = instance.GetType(); type != null; type = type.BaseType) {
                FieldInfo field = type.GetField(name, Flags | BindingFlags.DeclaredOnly);
                if (field != null) return field.GetValue(instance);
            }
            return null;
        }
        static void SeedCurrentSelection() {
            try {
                // Read the existing 2D manager only; never create a map object or select an army.
                if (Convert.ToBoolean(Field(Find("Tenth.Map.MapInterface"), null, "Is3dMapSelected"))) return;
                object manager = Field(Find("Tenth.Map.Map2d.MapArmyAnimationMgr"), null, "_sInstance");
                if (manager == null) return;
                object id = InheritedField(manager, "selectedId"), type = InheritedField(manager, "selectedType");
                if (id != null && type != null) Selected(Convert.ToInt32(id), type);
            } catch (InvalidOperationException) { } catch (MissingFieldException) { }
        }
        static void Selected(int id, object type) {
            try { lock (Gate) {
                // Army groups have a different ID namespace and cannot identify one personal army.
                string name = type == null ? "" : type.ToString();
                army = group = 0;
                if ((name != "PersonalArmy" && name != "GroupArmy") || id <= 0 || id > 500050009) {
                    kind = "unsupported_selection";
                } else if (name == "GroupArmy") { group = id; kind = "selected_group"; }
                else { army = id; kind = "selected"; }
                sequence++; selectedAt = Now();
                if (army > 0 || group > 0) { lastArmy = army; lastGroup = group; lastSequence = sequence; lastSelectedAt = selectedAt; }
            } } catch { /* A listener must never interrupt the game's selection. */ }
        }
        static void Cancelled(int id, object type) {
            try { lock (Gate) {
                string name = type == null ? "" : type.ToString();
                if (name == "GroupArmy" ? group != id : army != id) return;
                army = group = 0; kind = "cancelled"; sequence++; selectedAt = Now();
            } } catch { }
        }
        public static void Start(int expectedUserA, int expectedUserB, int expectedServer) {
            lock (Gate) {
                Stop();
                if (expectedUserA <= 0 || expectedUserB <= 0 || expectedServer <= 0)
                    throw new ArgumentException("Expected a verified role and server");
                userA = expectedUserA; userB = expectedUserB; server = expectedServer;
                Verify();
                Type type = Find("Tenth.Map.MapEventManager");
                events = Field(type, null, "sInstance");
                if (events == null) throw new InvalidOperationException("Map event manager not ready");
                selectedEvent = type.GetEvent("EventMapArmySelected", Flags);
                cancelledEvent = type.GetEvent("EventMapArmyCancelSelected", Flags);
                if (selectedEvent == null || cancelledEvent == null
                    || selectedEvent.EventHandlerType != typeof(Action<int, object>)
                    || cancelledEvent.EventHandlerType != typeof(Action<int, object>))
                    throw new InvalidOperationException("Unexpected map selection events");
                selectedHandler = Selected; cancelledHandler = Cancelled;
                readerId = Guid.NewGuid().ToString("N"); sequence = 0; army = 0; selectedAt = 0; kind = "waiting";
                lastArmy = lastGroup = group = 0; lastSequence = 0; lastSelectedAt = 0;
                try {
                    selectedEvent.AddEventHandler(events, selectedHandler);
                    cancelledEvent.AddEventHandler(events, cancelledHandler);
                    SeedCurrentSelection();
                } catch { Stop(); throw; }
            }
        }
        public static string Read() {
            lock (Gate) {
                Verify();
                if (events == null || !object.ReferenceEquals(events, Field(Find("Tenth.Map.MapEventManager"), null, "sInstance")))
                    throw new InvalidOperationException("Map event manager changed");
                return "{\"reader_id\":\"" + readerId + "\",\"sequence\":" + sequence.ToString(CultureInfo.InvariantCulture)
                    + ",\"army_id\":" + (army > 0 ? "\"" + army.ToString(CultureInfo.InvariantCulture) + "\"" : "null")
                    + ",\"group_id\":" + (group > 0 ? "\"" + group.ToString(CultureInfo.InvariantCulture) + "\"" : "null")
                    + ",\"selected_at\":" + selectedAt.ToString("R", CultureInfo.InvariantCulture)
                    + ",\"last_army_id\":" + (lastArmy > 0 ? "\"" + lastArmy.ToString(CultureInfo.InvariantCulture) + "\"" : "null")
                    + ",\"last_group_id\":" + (lastGroup > 0 ? "\"" + lastGroup.ToString(CultureInfo.InvariantCulture) + "\"" : "null")
                    + ",\"last_sequence\":" + lastSequence.ToString(CultureInfo.InvariantCulture)
                    + ",\"last_selected_at\":" + lastSelectedAt.ToString("R", CultureInfo.InvariantCulture)
                    + ",\"status\":\"" + kind + "\",\"union_relations\":" + Relations()
                    + ",\"relation_colors\":" + RelationColors() + ",\"game_requests_sent\":0}";
            }
        }
        static object Property(Type type, string name) {
            PropertyInfo property = type.GetProperty(name, Flags);
            if (property == null) throw new MissingMemberException(type.FullName, name);
            return property.GetValue(null, null);
        }
        static string N(object value) { return Convert.ToInt64(value).ToString(CultureInfo.InvariantCulture); }
        static string ColorHex(object value) {
            if (value == null) throw new InvalidOperationException("Map color missing");
            return "#" + Int(value,"R").ToString("x2") + Int(value,"G").ToString("x2") + Int(value,"B").ToString("x2");
        }
        static string RelationColors() {
            try {
                double at = Now();
                int uid = Convert.ToInt32(Property(Find("Tenth.Data.UserData"),"UserId"));
                int union = Convert.ToInt32(Property(Find("Tenth.Data.UserData"),"UnionID"));
                if ((uid != userA && uid != userB) || union < 0) return "{\"status\":\"scope_changed\"}";
                // Read the existing singleton's active mode; never create one or change graphics settings.
                Type facade = Find("Tenth.Map.MapCommonFacade");
                object instance = null;
                for (Type baseType = facade; baseType != null; baseType = baseType.BaseType) {
                    FieldInfo singleton = baseType.GetField("sInstance",Flags | BindingFlags.DeclaredOnly);
                    if (singleton != null && singleton.IsStatic) { instance = singleton.GetValue(null); break; }
                }
                if (instance == null) return "{\"status\":\"waiting\"}";
                int mode = Int(instance,"mColorType");
                if (mode < 0 || mode > 3) throw new InvalidOperationException("Unknown map color mode");
                Type define = Find("Tenth.Data.MapResDefine");
                string table = "sColorGridDef" + (mode == 1 ? "_P" : mode == 2 ? "_D" : mode == 3 ? "_T" : "");
                IDictionary colors = (IDictionary)Property(define,table);
                IDictionary relations = (IDictionary)Field(define,null,"sRelationDef");
                string[] keys = {"self","union","other","enemy","bandit","friend_other","friend_bandit"};
                List<string> rows = new List<string>();
                foreach (string key in keys) {
                    string[] names = (string[])relations[key];
                    if (names == null || names.Length != 3) throw new InvalidOperationException("Unknown relation palette");
                    rows.Add("\"" + key + "\":{\"inside\":\"" + ColorHex(colors[names[0]])
                        + "\",\"outside\":\"" + ColorHex(colors[names[1]]) + "\"}");
                }
                Verify();
                if (mode != Int(instance,"mColorType")) return "{\"status\":\"busy\"}";
                return "{\"status\":\"ready\",\"source\":\"client_map_palette\",\"user_id\":" + N(uid)
                    + ",\"union_id\":" + N(union) + ",\"server_id\":" + N(server) + ",\"color_mode\":" + N(mode)
                    + ",\"read_at\":" + at.ToString("R",CultureInfo.InvariantCulture)
                    + ",\"styles\":{" + string.Join(",",rows.ToArray()) + "},\"game_requests_sent\":0}";
            } catch (Exception error) { return "{\"status\":\"unavailable\",\"error_type\":\"" + error.GetType().Name + "\"}"; }
        }
        static string RelationRows(IDictionary data) {
            if (data == null || data.Count > 512) throw new InvalidOperationException("Relation cache limit");
            List<string> rows = new List<string>();
            foreach (DictionaryEntry item in data) {
                object row = item.Value; Type type = row.GetType();
                rows.Add("{\"id\":" + N(item.Key) + ",\"union_id\":" + N(Field(type,row,"union_id"))
                    + ",\"target_union_id\":" + N(Field(type,row,"target_union_id"))
                    + ",\"relationship\":" + N(Field(type,row,"relationship"))
                    + ",\"next_time\":" + N(Field(type,row,"next_time"))
                    + ",\"cron_time\":" + N(Field(type,row,"cron_time")) + "}");
            }
            return "[" + string.Join(",",rows.ToArray()) + "]";
        }
        static string Relations() {
            string stage = "binding";
            try {
                double at = Now();
                Type user = Find("Tenth.Data.UserData"), type = Find("Tenth.Data.UnionRelationData");
                int uid = Convert.ToInt32(Property(user,"UserId"));
                int union = Convert.ToInt32(Property(user,"UnionID"));
                if ((uid != userA && uid != userB) || union < 0) return "{\"status\":\"scope_changed\"}";
                object instance = Field(type,null,"sInstance");
                if (instance == null) return "{\"status\":\"waiting\"}";
                stage = "mode";
                bool directional = Convert.ToBoolean(Property(Find("Tenth.Data.SysParamData"),"IsRealMarchOpen"));
                stage = "cache";
                IDictionary raw = (IDictionary)Field(type,instance,"mCacheDict");
                string before = RelationRows(raw);
                stage = "effective";
                IDictionary resolved = (IDictionary)Field(type,instance,"mMyToOtherRealRelations");
                if (resolved == null || resolved.Count > 512) throw new InvalidOperationException("Relation result limit");
                List<string> pairs = new List<string>();
                foreach (DictionaryEntry pair in resolved) pairs.Add("[" + N(pair.Key) + "," + N(pair.Value) + "]");
                if (before != RelationRows(raw)) return "{\"status\":\"busy\"}";
                return "{\"status\":\"ready\",\"user_id\":" + N(uid) + ",\"union_id\":" + N(union)
                    + ",\"server_id\":" + N(server) + ",\"directional\":" + (directional ? "true" : "false")
                    + ",\"read_at\":" + at.ToString("R",CultureInfo.InvariantCulture)
                    + ",\"effective\":[" + string.Join(",",pairs.ToArray()) + "],\"records\":" + before
                    + ",\"game_requests_sent\":0}";
            } catch (Exception error) { return "{\"status\":\"unavailable\",\"stage\":\"" + stage
                + "\",\"error_type\":\"" + error.GetType().Name + "\"}"; }
        }
        public static void Stop() {
            lock (Gate) {
                if (events != null) {
                    if (selectedEvent != null && selectedHandler != null) selectedEvent.RemoveEventHandler(events, selectedHandler);
                    if (cancelledEvent != null && cancelledHandler != null) cancelledEvent.RemoveEventHandler(events, cancelledHandler);
                }
                events = null; selectedHandler = cancelledHandler = null; army = 0;
            }
        }
    }
}
