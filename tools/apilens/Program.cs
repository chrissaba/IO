// apilens: what a .NET library really offers, read from its metadata (no code in it is loaded or run): one type in full,
// a search over type and member names, or an overview of its namespaces. IO's api_lookup action writes a JSON request
// and reads the text this prints. Signatures come out in C# syntax with short type names; summaries come from the XML
// doc file beside the dll when there is one; [Obsolete] messages are shown, since they usually name the replacement.
//
// Request: {"search": [dlls to look in], "refs": [dlls only used to resolve types], "mode": "type|find|overview",
//           "query": "...", "member": "...", "max": 12000}
using System.Globalization;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;

static class Program
{
    sealed class Request
    {
        public List<string> Search { get; set; } = [];
        public List<string> Refs { get; set; } = [];
        public string Mode { get; set; } = "overview";
        public string Query { get; set; } = "";
        public string Member { get; set; } = "";
        public int Max { get; set; } = 12000;
    }

    const BindingFlags Declared = BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;
    static readonly StringBuilder Out = new();
    static int Max = 12000;
    static bool Full;
    static readonly Dictionary<Assembly, Type[]> TypesOf = [];
    static readonly HashSet<Assembly> Searched = [];

    static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        var req = JsonSerializer.Deserialize<Request>(File.ReadAllText(args[0]), new JsonSerializerOptions { PropertyNameCaseInsensitive = true })!;
        Max = Math.Max(1000, req.Max);

        var paths = new List<string>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        void Add(string p)
        {
            try
            {
                var full = Path.GetFullPath(p);
                if (File.Exists(full) && seen.Add(full)) paths.Add(full);
            }
            catch (Exception) { }
        }
        foreach (var p in req.Search) Add(p);
        foreach (var p in req.Refs) Add(p);
        // a library's own dependencies usually sit beside it
        foreach (var dir in req.Search.Select(Path.GetDirectoryName).Where(d => !string.IsNullOrEmpty(d)).Distinct())
            foreach (var f in Directory.EnumerateFiles(dir!, "*.dll")) Add(f);
        // a runtime for System.* when the refs carry none (a bare dll or folder rather than a project)
        if (!paths.Any(p => Path.GetFileName(p).Equals("System.Runtime.dll", StringComparison.OrdinalIgnoreCase)))
        {
            var rt = RuntimeEnvironment.GetRuntimeDirectory().TrimEnd('\\', '/');
            foreach (var f in Directory.EnumerateFiles(rt, "*.dll")) Add(f);
            var shared = Path.GetDirectoryName(Path.GetDirectoryName(rt))!;
            foreach (var other in new[] { "Microsoft.WindowsDesktop.App", "Microsoft.AspNetCore.App" })
            {
                var d = Path.Combine(shared, other, Path.GetFileName(rt));
                if (Directory.Exists(d)) foreach (var f in Directory.EnumerateFiles(d, "*.dll")) Add(f);
            }
        }

        using var mlc = new MetadataLoadContext(new PathAssemblyResolver(paths));
        var asms = new List<Assembly>();
        foreach (var p in req.Search)
        {
            try
            {
                var a = mlc.LoadFromAssemblyPath(Path.GetFullPath(p));
                if (Searched.Add(a)) asms.Add(a);
            }
            catch (Exception) { } // a native dll, or a second copy of one already loaded
        }
        if (asms.Count == 0)
        {
            Console.Write("None of those files is a .NET assembly.");
            return 1;
        }

        switch (req.Mode)
        {
            case "type": ShowType(asms, req.Query, req.Member); break;
            case "find": Find(asms, req.Query, header: true); break;
            default: Overview(asms); break;
        }
        Console.Write(Out.ToString().TrimEnd());
        return 0;
    }

    // ---------------------------------------------------------------------------------------------------- output

    static void Line(string s)
    {
        if (Full) return;
        if (Out.Length + s.Length > Max)
        {
            Out.AppendLine("[... more left out: narrow it with member=, a more specific type, or find=]");
            Full = true;
            return;
        }
        Out.AppendLine(s);
    }

    static string Clip(string s, int n) => s.Length <= n ? s : s[..(n - 1)].TrimEnd() + "…";

    static string Libs(IEnumerable<Assembly> asms)
    {
        var names = asms.Select(a => $"{a.GetName().Name} {a.GetName().Version}").ToList();
        return string.Join(", ", names.Take(8)) + (names.Count > 8 ? $" and {names.Count - 8} more" : "");
    }

    // ---------------------------------------------------------------------------------------------------- types

    static Type[] Exported(Assembly a)
    {
        if (TypesOf.TryGetValue(a, out var have)) return have;
        Type[] types;
        try { types = a.GetExportedTypes(); }
        catch (ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null && (t.IsPublic || t.IsNestedPublic)).ToArray()!; }
        catch (Exception)
        {
            try { types = a.GetTypes().Where(t => t.IsPublic || t.IsNestedPublic).ToArray(); }
            catch (Exception) { types = []; }
        }
        TypesOf[a] = types;
        return types;
    }

    static string Plain(string name)
    {
        var i = name.IndexOf('`');
        return i < 0 ? name : name[..i];
    }

    /// <summary>The type's name as code writes it: Namespace.Outer.Inner&lt;T&gt; (full) or Inner&lt;T&gt;.</summary>
    static string Display(Type t, bool full)
    {
        var name = Plain(t.Name);
        if (t.IsGenericTypeDefinition)
        {
            var own = t.GetGenericArguments().Skip(t.IsNested && t.DeclaringType!.IsGenericTypeDefinition ? t.DeclaringType.GetGenericArguments().Length : 0)
                .Select(a => a.Name).ToList();
            if (own.Count > 0) name += "<" + string.Join(", ", own) + ">";
        }
        if (t.IsNested && t.DeclaringType != null) return Display(t.DeclaringType, full) + "." + name;
        return full && !string.IsNullOrEmpty(t.Namespace) ? t.Namespace + "." + name : name;
    }

    static readonly Dictionary<string, string> Alias = new()
    {
        ["System.Void"] = "void", ["System.Boolean"] = "bool", ["System.Byte"] = "byte", ["System.SByte"] = "sbyte",
        ["System.Int16"] = "short", ["System.UInt16"] = "ushort", ["System.Int32"] = "int", ["System.UInt32"] = "uint",
        ["System.Int64"] = "long", ["System.UInt64"] = "ulong", ["System.Single"] = "float", ["System.Double"] = "double",
        ["System.Decimal"] = "decimal", ["System.Char"] = "char", ["System.String"] = "string", ["System.Object"] = "object",
        ["System.IntPtr"] = "nint", ["System.UIntPtr"] = "nuint",
    };

    /// <summary>A type as it would be written in a signature (short names, C# keywords).</summary>
    static string N(Type t)
    {
        try
        {
            if (t.IsByRef) return N(t.GetElementType()!);
            if (t.IsPointer) return N(t.GetElementType()!) + "*";
            if (t.IsArray) return N(t.GetElementType()!) + "[" + new string(',', t.GetArrayRank() - 1) + "]";
            if (t.IsGenericParameter) return t.Name;
            if (t.IsFunctionPointer)
                return "delegate* unmanaged<" + string.Join(", ", t.GetFunctionPointerParameterTypes().Select(N).Append(N(t.GetFunctionPointerReturnType()))) + ">";
            if (t.FullName != null && Alias.TryGetValue(t.FullName, out var k)) return k;
            if (t.IsGenericType && !t.IsGenericTypeDefinition)
            {
                var def = t.GetGenericTypeDefinition();
                var args = t.GetGenericArguments();
                if (def.FullName == "System.Nullable`1") return N(args[0]) + "?";
                if (def.FullName != null && def.FullName.StartsWith("System.ValueTuple`")) return "(" + string.Join(", ", args.Select(N)) + ")";
                var outer = def.IsNested && def.DeclaringType != null ? Plain(def.DeclaringType.Name) + "." : "";
                var skip = def.IsNested && def.DeclaringType!.IsGenericTypeDefinition ? def.DeclaringType.GetGenericArguments().Length : 0;
                var own = args.Skip(skip).ToList();
                return outer + Plain(def.Name) + (own.Count > 0 ? "<" + string.Join(", ", own.Select(N)) + ">" : "");
            }
            return Display(t, full: false);
        }
        catch (Exception)
        {
            return "?";
        }
    }

    static bool IsDelegate(Type t) => t.BaseType?.FullName == "System.MulticastDelegate";

    static string Kind(Type t)
    {
        if (t.IsInterface) return "interface";
        if (t.IsEnum) return "enum";
        if (IsDelegate(t)) return "delegate";
        if (t.IsValueType) return "struct";
        if (t.IsAbstract && t.IsSealed) return "static class";
        if (t.IsAbstract) return "abstract class";
        return t.IsSealed ? "sealed class" : "class";
    }

    static bool IsFramework(Assembly a)
    {
        var n = a.GetName().Name ?? "";
        return n is "mscorlib" or "netstandard" || n.StartsWith("System") || n.StartsWith("Microsoft.");
    }

    // ---------------------------------------------------------------------------------------------------- attributes

    static IEnumerable<CustomAttributeData> Attrs(MemberInfo m)
    {
        try { return m.GetCustomAttributesData(); }
        catch (Exception) { return []; }
    }

    /// <summary>[Obsolete: why] / [Experimental]: the library's own word that something is going away or is new.</summary>
    static string Flags(MemberInfo m)
    {
        var parts = new List<string>();
        foreach (var a in Attrs(m))
        {
            var name = a.AttributeType.FullName;
            if (name == "System.ObsoleteAttribute")
            {
                var msg = a.ConstructorArguments.Count > 0 ? a.ConstructorArguments[0].Value as string : null;
                var error = a.ConstructorArguments.Count > 1 && a.ConstructorArguments[1].Value is true;
                parts.Add($"[Obsolete{(error ? ", an error to use" : "")}{(string.IsNullOrWhiteSpace(msg) ? "" : ": " + msg)}]");
            }
            else if (name == "System.Diagnostics.CodeAnalysis.ExperimentalAttribute")
                parts.Add("[Experimental]");
        }
        return parts.Count > 0 ? " " + string.Join(" ", parts) : "";
    }

    static string Offset(FieldInfo f)
    {
        foreach (var a in Attrs(f))
            if (a.AttributeType.FullName == "System.Runtime.InteropServices.FieldOffsetAttribute" && a.ConstructorArguments.Count > 0 && a.ConstructorArguments[0].Value is int o)
                return $"[0x{o:X}] ";
        return "";
    }

    static bool Has(MemberInfo m, string attr) => Attrs(m).Any(a => a.AttributeType.FullName == attr);

    // ---------------------------------------------------------------------------------------------------- signatures

    static string Value(object? v) => v switch
    {
        null => "null",
        string s => "\"" + s.Replace("\"", "\\\"") + "\"",
        bool b => b ? "true" : "false",
        char c => $"'{c}'",
        IFormattable f => f.ToString(null, CultureInfo.InvariantCulture),
        _ => v.ToString() ?? "",
    };

    static string Params(MethodBase m)
    {
        var ext = m.IsStatic && Has(m, "System.Runtime.CompilerServices.ExtensionAttribute");
        return string.Join(", ", m.GetParameters().Select((p, i) =>
        {
            var mod = "";
            if (p.ParameterType.IsByRef) mod = p.IsOut ? "out " : p.IsIn ? "in " : "ref ";
            if (i == 0 && ext) mod = "this " + mod;
            try
            {
                if (p.GetCustomAttributesData().Any(a => a.AttributeType.FullName == "System.ParamArrayAttribute")) mod += "params ";
            }
            catch (Exception) { }
            var def = "";
            try
            {
                if (p.HasDefaultValue) def = " = " + Value(p.RawDefaultValue);
            }
            catch (Exception) { def = " = ..."; }
            return mod + N(p.ParameterType) + " " + p.Name + def;
        }));
    }

    static string GenericArgs(MethodInfo m) => m.IsGenericMethodDefinition ? "<" + string.Join(", ", m.GetGenericArguments().Select(a => a.Name)) + ">" : "";

    static string MemberText(MemberInfo m, Type owner)
    {
        switch (m)
        {
            case ConstructorInfo c:
                return $"new {Plain(owner.Name)}({Params(c)})";
            case MethodInfo mi:
                return (mi.IsStatic && !owner.IsInterface ? "static " : mi.IsStatic ? "static abstract " : "") +
                       $"{N(mi.ReturnType)} {mi.Name}{GenericArgs(mi)}({Params(mi)})";
            case PropertyInfo p:
            {
                var get = p.GetGetMethod();
                var set = p.GetSetMethod();
                var stat = (get ?? set)?.IsStatic == true ? "static " : "";
                var idx = p.GetIndexParameters();
                var name = idx.Length > 0 ? "this[" + string.Join(", ", idx.Select(i => N(i.ParameterType) + " " + i.Name)) + "]" : p.Name;
                var acc = (get != null ? "get; " : "") + (set != null ? (IsInit(set) ? "init; " : "set; ") : "");
                return $"{stat}{N(p.PropertyType)} {name} {{ {acc}}}";
            }
            case FieldInfo f:
                if (f.IsLiteral)
                {
                    object? v = null;
                    try { v = f.GetRawConstantValue(); } catch (Exception) { }
                    return $"const {N(f.FieldType)} {f.Name} = {Value(v)}";
                }
                return Offset(f) + (f.IsStatic ? "static " : "") + (f.IsInitOnly ? "readonly " : "") + $"{N(f.FieldType)} {f.Name}";
            case EventInfo e:
                return (e.AddMethod?.IsStatic == true ? "static " : "") + $"event {N(e.EventHandlerType!)} {e.Name}";
            case Type t:
                return $"{Kind(t)} {Display(t, false)} (nested)";
        }
        return m.Name;
    }

    static bool IsInit(MethodInfo set)
    {
        try
        {
            return set.ReturnParameter.GetRequiredCustomModifiers().Any(t => t.FullName == "System.Runtime.CompilerServices.IsExternalInit");
        }
        catch (Exception)
        {
            return false;
        }
    }

    static IEnumerable<MemberInfo> MembersOf(Type t)
    {
        MemberInfo[] all;
        try { all = t.GetMembers(Declared); }
        catch (Exception) { return []; }
        return all.Where(m => m switch
        {
            MethodInfo mi => !mi.IsSpecialName,                                  // property/event accessors, operators
            FieldInfo f => !f.IsSpecialName && !(t.IsEnum && !f.IsStatic),         // an enum's value__
            ConstructorInfo c => !c.IsStatic && !t.IsInterface && !(t.IsAbstract && t.IsSealed),
            _ => true,
        });
    }

    static int Order(MemberInfo m) => m switch
    {
        FieldInfo { IsLiteral: true } => 0,
        ConstructorInfo => 1,
        PropertyInfo => 2,
        FieldInfo => 3,
        EventInfo => 4,
        MethodInfo => 5,
        _ => 6,
    };

    // ---------------------------------------------------------------------------------------------------- docs

    static readonly Dictionary<string, Dictionary<string, string>> DocCache = [];

    /// <summary>The library's XML doc summaries: "Namespace.Type" or "Namespace.Type.Member" -> one line of text.</summary>
    static Dictionary<string, string> Docs(Assembly a)
    {
        var loc = a.Location;
        if (DocCache.TryGetValue(loc, out var have)) return have;
        var docs = new Dictionary<string, string>(StringComparer.Ordinal);
        DocCache[loc] = docs;
        var xml = Path.ChangeExtension(loc, ".xml");
        if (!File.Exists(xml)) return docs;
        try
        {
            using var r = XmlReader.Create(xml, new XmlReaderSettings { DtdProcessing = DtdProcessing.Ignore, XmlResolver = null, IgnoreComments = true });
            r.MoveToContent();
            while (!r.EOF)
            {
                if (r.NodeType == XmlNodeType.Element && r.Name == "member")
                {
                    if (XNode.ReadFrom(r) is XElement el)
                    {
                        var id = (string?)el.Attribute("name");
                        var summary = el.Element("summary");
                        if (id is { Length: > 2 } && summary != null)
                        {
                            var key = DocKey(id);
                            var text = Clip(Regex.Replace(Text(summary), @"\s+", " ").Trim(), 220);
                            if (text.Length > 0) docs.TryAdd(key, text);
                        }
                    }
                }
                else r.Read();
            }
        }
        catch (Exception) { }
        return docs;
    }

    /// <summary>"M:Ns.Type.Method``1(System.Int32)" -> "Ns.Type.Method" (overloads share one summary here).</summary>
    static string DocKey(string id)
    {
        var s = id.Length > 2 && id[1] == ':' ? id[2..] : id;
        var paren = s.IndexOf('(');
        if (paren >= 0) s = s[..paren];
        return Regex.Replace(s, @"``\d+", "");
    }

    static string Text(XElement e)
    {
        var sb = new StringBuilder();
        foreach (var n in e.Nodes())
        {
            if (n is XText t) sb.Append(t.Value);
            else if (n is XElement x)
            {
                var cref = (string?)x.Attribute("cref");
                var word = (string?)x.Attribute("langword") ?? (string?)x.Attribute("name");
                if (cref != null) sb.Append(Regex.Replace(DocKey(cref).Split('.').Last(), @"`\d+", ""));
                else if (word != null && x.IsEmpty) sb.Append(word);
                else sb.Append(Text(x));
                if (x.Name.LocalName == "para") sb.Append(' ');
            }
        }
        return sb.ToString();
    }

    static string TypeKey(Type t) => (t.FullName ?? Display(t, true)).Replace('+', '.');

    static string Doc(Dictionary<string, string> docs, string key, int width) =>
        docs.TryGetValue(key, out var d) ? "  // " + Clip(d, width) : "";

    // ---------------------------------------------------------------------------------------------------- type mode

    static List<Type> Matching(List<Assembly> asms, string q)
    {
        var hits = new List<(int rank, int order, Type t)>();
        var order = 0;
        foreach (var a in asms)
            foreach (var t in Exported(a))
            {
                order++;
                var full = Regex.Replace(TypeKey(t), @"`\d+", "");
                var name = Plain(t.Name);
                if (name == q || full == q) hits.Add((0, order, t));
                else if (name.Equals(q, StringComparison.OrdinalIgnoreCase) || full.Equals(q, StringComparison.OrdinalIgnoreCase)) hits.Add((1, order, t));
                else if (full.EndsWith("." + q, StringComparison.OrdinalIgnoreCase)) hits.Add((2, order, t));
            }
        return hits.OrderBy(h => h.rank).ThenBy(h => h.order).Select(h => h.t).ToList();
    }

    static void ShowType(List<Assembly> asms, string query, string member)
    {
        var q = Regex.Replace(query.Trim().Replace('+', '.'), @"<.*>$", "");
        q = Regex.Replace(q, @"`\d+$", "");
        var found = Matching(asms, q);
        if (found.Count == 0 && q.Contains('.') && member.Length == 0)
        {
            var cut = q.LastIndexOf('.');
            var owner = Matching(asms, q[..cut]);
            if (owner.Count > 0)
            {
                found = owner;
                member = q[(cut + 1)..];
            }
        }
        if (found.Count == 0)
        {
            Line($"No type named {query} in {Libs(asms)}. Names that contain it:");
            Find(asms, q.Split('.').Last(), header: false);
            return;
        }
        foreach (var t in found.Take(member.Length > 0 ? 5 : 3))
        {
            WriteType(t, member);
            Line("");
        }
        if (found.Count > 3)
            Line("Also named like that: " + string.Join(", ", found.Skip(3).Take(15).Select(t => $"{Kind(t)} {Display(t, true)}")));
    }

    static void WriteType(Type t, string member)
    {
        var docs = Docs(t.Assembly);
        var bases = new List<string>();
        if (t.BaseType != null && t.BaseType.FullName is not ("System.Object" or "System.ValueType" or "System.Enum" or "System.MulticastDelegate"))
            bases.Add(N(t.BaseType));
        Type[] ifaces = [];
        try
        {
            var inheritedIfaces = t.BaseType?.GetInterfaces() ?? [];
            ifaces = t.GetInterfaces().Where(i => !inheritedIfaces.Contains(i)).ToArray();
        }
        catch (Exception) { }
        bases.AddRange(ifaces.Select(N));
        var name = t.Assembly.GetName();
        Line($"{Kind(t)} {Display(t, true)}{(bases.Count > 0 ? " : " + string.Join(", ", bases) : "")}   ({name.Name} {name.Version}){Flags(t)}");
        var summary = Doc(docs, TypeKey(t), 400);
        if (summary.Length > 0) Line(summary.TrimStart());

        if (IsDelegate(t))
        {
            var inv = t.GetMethod("Invoke");
            if (inv != null) Line($"  delegate {N(inv.ReturnType)} {Plain(t.Name)}({Params(inv)})");
            return;
        }

        // the type's own members, then what it gets from the library's own base types and interfaces
        var sources = new List<Type> { t };
        try
        {
            if (t.IsInterface) sources.AddRange(t.GetInterfaces().Where(i => !IsFramework(i.Assembly)));
            else
                for (var b = t.BaseType; b != null && !IsFramework(b.Assembly); b = b.BaseType)
                    sources.Add(b);
        }
        catch (Exception) { }

        var filter = member.Trim();
        if (filter.Length > 0 && !sources.SelectMany(MembersOf).Any(m => m.Name.Contains(filter, StringComparison.OrdinalIgnoreCase)))
        {
            Line($"  (no member named like \"{filter}\": all of them below)");
            filter = "";
        }
        var count = sources.Sum(s => MembersOf(s).Count());
        var width = count > 80 ? 90 : 160;
        foreach (var src in sources)
        {
            var members = MembersOf(src)
                .Where(m => filter.Length == 0 || m.Name.Contains(filter, StringComparison.OrdinalIgnoreCase))
                .OrderBy(Order).ThenBy(m => t.IsEnum ? 0 : 1).ToList();
            if (members.Count == 0) continue;
            if (src != t) Line($"  from {Display(src, false)}:");
            var srcDocs = Docs(src.Assembly);
            var key = TypeKey(src);
            foreach (var m in members)
            {
                var text = t.IsEnum && m is FieldInfo f ? $"{f.Name} = {Value(SafeConst(f))}" : MemberText(m, src);
                var docKey = key + "." + (m is ConstructorInfo ? "#ctor" : m.Name);
                Line("    " + text + Flags(m) + Doc(srcDocs, docKey, width));
                if (Full) return;
            }
        }
    }

    static object? SafeConst(FieldInfo f)
    {
        try { return f.GetRawConstantValue(); }
        catch (Exception) { return null; }
    }

    // ---------------------------------------------------------------------------------------------------- find mode

    /// <summary>0: the exact name, 1: starts with it, 2: has all its words; -1: no match. "local player" matches LocalPlayer.</summary>
    static int Rank(string name, string q, string[] words)
    {
        if (name.Equals(q, StringComparison.OrdinalIgnoreCase)) return 0;
        if (name.StartsWith(q, StringComparison.OrdinalIgnoreCase)) return 1;
        return words.All(w => name.Contains(w, StringComparison.OrdinalIgnoreCase)) ? 2 : -1;
    }

    // generated plumbing beside the real members (FFXIVClientStructs' Addresses, MemberFunctionPointers, Delegates...):
    // matches there come after the real ones
    static readonly Regex Plumbing = new(@"^(Addresses|MemberFunctionPointers|StaticAddressPointers|Delegates|.*VirtualTable.*|<.*)$");

    static void Find(List<Assembly> asms, string query, bool header)
    {
        var q = Regex.Replace(query.Trim(), @"\s+", "");
        var words = query.Split([' ', '.', '_'], StringSplitOptions.RemoveEmptyEntries);
        if (words.Length == 0) words = [q];
        var types = new List<(int rank, int order, Type t)>();
        var members = new List<(int rank, int order, Type t, MemberInfo m)>();
        var order = 0;
        foreach (var a in asms)
            foreach (var t in Exported(a))
            {
                order++;
                var at = order + (t.IsNested && (Plumbing.IsMatch(t.Name) || (t.DeclaringType != null && Plumbing.IsMatch(t.DeclaringType.Name))) ? 1_000_000 : 0);
                var r = Rank(Plain(t.Name), q, words);
                if (r >= 0) types.Add((r, at, t));
                MemberInfo[] all;
                try { all = t.GetMembers(Declared); }
                catch (Exception) { continue; }
                foreach (var m in all)
                {
                    if (m is MethodBase { IsSpecialName: true } || m is ConstructorInfo || m is Type) continue;
                    var rm = Rank(m.Name, q, words);
                    if (rm >= 0) members.Add((rm, at, t, m));
                }
            }
        if (header) Line($"Searched {Libs(asms)} for \"{query}\":");
        if (types.Count == 0 && members.Count == 0)
        {
            Line("Nothing by that name. Try a shorter word, or no type/find to see the namespaces.");
            return;
        }
        void Types()
        {
            if (types.Count == 0) return;
            Line($"types ({types.Count}):");
            foreach (var (_, _, t) in types.OrderBy(h => h.rank).ThenBy(h => h.order).Take(40))
                Line($"  {Kind(t)} {Display(t, true)}{Flags(t)}");
        }
        void Members()
        {
            if (members.Count == 0) return;
            Line($"members ({members.Count}):");
            foreach (var (_, _, t, m) in members.OrderBy(h => h.rank).ThenBy(h => h.order).Take(60))
                Line($"  {Display(t, true)}: {MemberText(m, t)}{Flags(m)}");
        }
        // the better match first: an exact member name beats types that merely contain the word
        int Best<T>(List<T> hits, Func<T, (int, int)> key) => hits.Count == 0 ? int.MaxValue : hits.Min(h => key(h).Item1 * 10 + (key(h).Item2 >= 1_000_000 ? 5 : 0));
        if (Best(members, h => (h.rank, h.order)) < Best(types, h => (h.rank, h.order)))
        {
            Members();
            Types();
        }
        else
        {
            Types();
            Members();
        }
        if (types.Count > 40 || members.Count > 60) Line("[more matches: use a longer word]");
    }

    // ---------------------------------------------------------------------------------------------------- overview

    static void Overview(List<Assembly> asms)
    {
        var share = Max / Math.Max(1, asms.Count);
        foreach (var a in asms)
        {
            var types = Exported(a).Where(t => !t.IsNested).ToArray();
            if (types.Length == 0) continue;
            var start = Out.Length;
            Line($"{a.GetName().Name} {a.GetName().Version}: {types.Length} public types");
            foreach (var g in types.GroupBy(t => t.Namespace ?? "").OrderBy(g => g.Key, StringComparer.Ordinal))
            {
                var names = g.OrderBy(t => t.IsInterface ? 0 : 1).ThenBy(t => t.Name, StringComparer.Ordinal).Select(t => Display(t, false)).ToList();
                Line($"  {(g.Key.Length > 0 ? g.Key : "(no namespace)")} ({names.Count}): {string.Join(", ", names.Take(24))}{(names.Count > 24 ? ", ..." : "")}");
                if (Full) return;
                if (Out.Length - start > share)
                {
                    Line("  [... more namespaces]");
                    break;
                }
            }
        }
    }
}
