using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;

namespace STLab.BattleReportReader
{
    // Only the three verified report reads; no generic commands or game orders.
    public static class Entry
    {
        private static readonly object Gate = new object();
        private static EventInfo frameEvent;
        private static Action handler;
        private static string phase = "new", error = "";
        private static int battleId, command, userA, userB, server, attempts;
        private static uint before, after;
        private static bool subscribed;
        private static readonly List<object> profiles = new List<object>();
        private static DateTime deadline;

        private static Type Find(string name)
        {
            string owner = name == "NeoX.Game" ? "Engine" : "Game.Login";
            Type found = null;
            foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                if (assembly.GetName().Name != owner) continue;
                Type type = assembly.GetType(name, false);
                if (type == null) continue;
                if (found != null && !object.ReferenceEquals(found, type)) throw new InvalidOperationException("Ambiguous client type");
                found = type;
            }
            if (found != null) return found;
            throw new InvalidOperationException("Required client type not loaded");
        }

        private static object Field(Type type, object instance, string name)
        {
            FieldInfo field = type.GetField(name, BindingFlags.Static | BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (field == null) throw new MissingFieldException(type.FullName, name);
            return field.GetValue(instance);
        }

        private static object Manager()
        {
            Type net = Find("Tenth.Network.NetManager");
            object instance = Field(net, null, "sInstance");
            if (instance == null) throw new InvalidOperationException("Role not logged in");
            object manager = Field(net, instance, "mCmdMgr");
            if (manager == null || manager.GetType().FullName != "Tenth.Network.DotnetCmdMgr")
                throw new InvalidOperationException("Unexpected client network manager");
            Type type = manager.GetType();
            int user = Convert.ToInt32(Field(type, manager, "_userId"));
            if ((user != userA && user != userB) || Convert.ToInt32(Field(type, manager, "_serverId")) != server
                || Convert.ToInt32(Field(type, manager, "_networkState")) != 1)
                throw new InvalidOperationException("Role/server/session binding changed");
            return manager;
        }

        public static void ClearProfiles()
        {
            lock (Gate) { if (subscribed) throw new InvalidOperationException("Read pending"); profiles.Clear(); }
        }
        public static void AddProfile(int id)
        {
            lock (Gate) {
                if (subscribed || id <= 0 || profiles.Count >= 15 || profiles.Contains(id))
                    throw new ArgumentException("Expected 1-15 distinct observed reports");
                profiles.Add(id);
            }
        }

        public static void Queue(int kind, int bid, int expectedUserA, int expectedUserB, int expectedServer)
        {
            lock (Gate)
            {
              try
              {
                if (kind < 0 || kind > 3 || bid < 0 || (kind == 1 && bid == 0) || (kind == 3 && bid != 0)
                    || (kind == 0 && (profiles.Count < 1 || profiles.Count > 15))
                    || expectedUserA <= 0 || expectedUserB <= 0 || expectedServer <= 0)
                    throw new ArgumentException("Only verified report and own member-list reads are permitted");
                if (subscribed || phase == "invoking") throw new InvalidOperationException("Read pending");
                battleId = bid; command = kind == 0 ? 10 : kind == 1 ? 11 : kind == 2 ? 92 : 103;
                userA = expectedUserA; userB = expectedUserB; server = expectedServer;
                Manager();
                frameEvent = Find("NeoX.Game").GetEvent("EventLogic", BindingFlags.Public | BindingFlags.Static);
                if (frameEvent == null || frameEvent.EventHandlerType != typeof(Action))
                    throw new InvalidOperationException("Client frame event unavailable");
                handler = Execute;
                phase = "queued"; error = ""; attempts = 0; before = after = 0;
                deadline = DateTime.UtcNow.AddSeconds(12);
                frameEvent.AddEventHandler(null, handler);
                subscribed = true;
              }
              catch (Exception ex)
              {
                  Unsubscribe();
                  phase = "failed"; error = ex.GetType().FullName + ": " + ex.Message;
              }
            }
        }

        private static void Unsubscribe()
        {
            if (subscribed)
            {
                frameEvent.RemoveEventHandler(null, handler);
                subscribed = false;
            }
        }

        private static void Execute()
        {
            lock (Gate)
            {
                if (phase != "queued") return;
                try
                {
                    Unsubscribe();
                    if (DateTime.UtcNow > deadline) { phase = "frame_timeout"; return; }
                    object manager = Manager();
                    Type type = manager.GetType();
                    MethodInfo send = type.GetMethod("SendRawObject", BindingFlags.Public | BindingFlags.Instance,
                        null, new Type[] { typeof(int), typeof(object) }, null);
                    if (send == null) throw new MissingMethodException("Client read method unavailable");
                    object[] body = command == 10
                        ? new object[] { profiles.ToArray(), 0, 0 }
                        : command == 11 ? new object[] { battleId, 0, 0 }
                        : command == 103 ? new object[] { }
                        : new object[] { new object[] { 1, 2 }, new object[] { 1, 3 }, new object[] { -1 }, battleId, 0 };
                    before = unchecked((uint)Convert.ToInt32(Field(type, manager, "_cmdIndex")));
                    phase = "invoking"; attempts = 1;
                    send.Invoke(manager, new object[] { command, body });
                    after = unchecked((uint)Convert.ToInt32(Field(type, manager, "_cmdIndex")));
                    // Some client builds dispatch after this call returns. Only
                    // the original-socket observer can establish actual sends.
                    phase = "submitted";
                }
                catch (Exception ex)
                {
                    phase = "failed";
                    Exception actual = ex is TargetInvocationException && ex.InnerException != null ? ex.InnerException : ex;
                    error = actual.GetType().FullName + ": " + actual.Message;
                }
            }
        }

        public static void Cancel()
        {
            lock (Gate) { Unsubscribe(); if (phase == "queued") phase = "cancelled"; }
        }

        public static string Status()
        {
            lock (Gate)
            {
                return "{\"phase\":\"" + phase + "\",\"attempts\":" + attempts.ToString(CultureInfo.InvariantCulture)
                    + ",\"gameRequestsSent\":null,\"requestSubmitted\":" + (phase == "submitted" ? "true" : "false")
                    + ",\"indexBefore\":" + before.ToString(CultureInfo.InvariantCulture)
                    + ",\"indexAfter\":" + after.ToString(CultureInfo.InvariantCulture)
                    + ",\"hooksInstalled\":0,\"subscribed\":" + (subscribed ? "true" : "false")
                    + ",\"command\":" + command.ToString(CultureInfo.InvariantCulture)
                    + ",\"battleId\":" + battleId.ToString(CultureInfo.InvariantCulture)
                    + ",\"error\":\"" + error.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", " ").Replace("\n", " ") + "\"}";
            }
        }
    }
}
