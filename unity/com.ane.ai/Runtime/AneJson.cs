// A small JSON reader and writer for the ANE AI protocol, so the package needs nothing else.
// Objects are Dictionary<string, object>, arrays List<object>, numbers long or double, and
// true/false/null bool/null.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace Ane.AI
{
    public static class AneJson
    {
        public static object Parse(string text)
        {
            if (text == null) throw new ArgumentNullException(nameof(text));
            var reader = new Reader(text);
            var value = reader.ReadValue();
            reader.SkipWhitespace();
            if (!reader.AtEnd) throw new FormatException($"Unexpected text after the JSON value at {reader.Position}.");
            return value;
        }

        public static Dictionary<string, object> ParseObject(string text)
        {
            if (string.IsNullOrWhiteSpace(text)) return new Dictionary<string, object>();
            if (Parse(text) is Dictionary<string, object> obj) return obj;
            throw new FormatException("The JSON is not an object.");
        }

        public static string Serialize(object value)
        {
            var builder = new StringBuilder();
            Write(builder, value);
            return builder.ToString();
        }

        // ---- convenient reading -------------------------------------------------------------

        public static string GetString(this IDictionary<string, object> obj, string key, string fallback = null)
            => obj != null && obj.TryGetValue(key, out var value) && value != null ? Convert.ToString(value, CultureInfo.InvariantCulture) : fallback;

        public static long GetLong(this IDictionary<string, object> obj, string key, long fallback = 0)
            => obj != null && obj.TryGetValue(key, out var value) && value != null ? Convert.ToInt64(value, CultureInfo.InvariantCulture) : fallback;

        public static int GetInt(this IDictionary<string, object> obj, string key, int fallback = 0)
            => (int)GetLong(obj, key, fallback);

        public static double GetDouble(this IDictionary<string, object> obj, string key, double fallback = 0)
            => obj != null && obj.TryGetValue(key, out var value) && value != null ? Convert.ToDouble(value, CultureInfo.InvariantCulture) : fallback;

        public static bool GetBool(this IDictionary<string, object> obj, string key, bool fallback = false)
            => obj != null && obj.TryGetValue(key, out var value) && value is bool b ? b : fallback;

        public static Dictionary<string, object> GetObject(this IDictionary<string, object> obj, string key)
            => obj != null && obj.TryGetValue(key, out var value) ? value as Dictionary<string, object> : null;

        public static List<object> GetList(this IDictionary<string, object> obj, string key)
            => obj != null && obj.TryGetValue(key, out var value) && value is List<object> list ? list : new List<object>();

        public static List<string> GetStrings(this IDictionary<string, object> obj, string key)
        {
            var result = new List<string>();
            foreach (var item in GetList(obj, key)) result.Add(Convert.ToString(item, CultureInfo.InvariantCulture));
            return result;
        }

        // ---- writing ----------------------------------------------------------------------------

        static void Write(StringBuilder builder, object value)
        {
            switch (value)
            {
                case null:
                    builder.Append("null");
                    return;
                case string text:
                    WriteString(builder, text);
                    return;
                case bool flag:
                    builder.Append(flag ? "true" : "false");
                    return;
                case IDictionary dictionary:
                {
                    builder.Append('{');
                    var first = true;
                    foreach (DictionaryEntry entry in dictionary)
                    {
                        if (!first) builder.Append(',');
                        first = false;
                        WriteString(builder, Convert.ToString(entry.Key, CultureInfo.InvariantCulture));
                        builder.Append(':');
                        Write(builder, entry.Value);
                    }
                    builder.Append('}');
                    return;
                }
                case IEnumerable sequence:
                {
                    builder.Append('[');
                    var first = true;
                    foreach (var item in sequence)
                    {
                        if (!first) builder.Append(',');
                        first = false;
                        Write(builder, item);
                    }
                    builder.Append(']');
                    return;
                }
                case float f:
                    if (float.IsNaN(f) || float.IsInfinity(f)) builder.Append("null");  // not JSON numbers
                    else WriteReal(builder, f.ToString("R", CultureInfo.InvariantCulture));
                    return;
                case double d:
                    if (double.IsNaN(d) || double.IsInfinity(d)) builder.Append("null");
                    else WriteReal(builder, d.ToString("R", CultureInfo.InvariantCulture));
                    return;
                case Enum e:
                    WriteString(builder, e.ToString());
                    return;
                case decimal m:
                    builder.Append(m.ToString(CultureInfo.InvariantCulture));
                    return;
                case IConvertible number when IsNumber(number):
                    builder.Append(Convert.ToString(number, CultureInfo.InvariantCulture));
                    return;
                default:
                    WriteString(builder, Convert.ToString(value, CultureInfo.InvariantCulture));
                    return;
            }
        }

        /// <summary>A real number stays one: 1.0 is written "1.0", not "1" (which reads back as an integer).</summary>
        static void WriteReal(StringBuilder builder, string text)
        {
            builder.Append(text);
            if (text.IndexOfAny(new[] { '.', 'E', 'e' }) < 0) builder.Append(".0");
        }

        static bool IsNumber(IConvertible value)
        {
            switch (value.GetTypeCode())
            {
                case TypeCode.SByte: case TypeCode.Byte: case TypeCode.Int16: case TypeCode.UInt16:
                case TypeCode.Int32: case TypeCode.UInt32: case TypeCode.Int64: case TypeCode.UInt64:
                    return true;
                default:
                    return false;
            }
        }

        static void WriteString(StringBuilder builder, string text)
        {
            builder.Append('"');
            foreach (var c in text)
            {
                switch (c)
                {
                    case '"': builder.Append("\\\""); break;
                    case '\\': builder.Append("\\\\"); break;
                    case '\n': builder.Append("\\n"); break;
                    case '\r': builder.Append("\\r"); break;
                    case '\t': builder.Append("\\t"); break;
                    case '\b': builder.Append("\\b"); break;
                    case '\f': builder.Append("\\f"); break;
                    default:
                        if (c < 0x20) builder.Append("\\u").Append(((int)c).ToString("x4"));
                        else builder.Append(c);
                        break;
                }
            }
            builder.Append('"');
        }

        // ---- reading ----------------------------------------------------------------------------

        sealed class Reader
        {
            readonly string text;
            int index;

            public Reader(string text) { this.text = text; }
            public int Position => index;
            public bool AtEnd => index >= text.Length;

            public void SkipWhitespace()
            {
                while (index < text.Length && char.IsWhiteSpace(text[index])) index++;
            }

            char Next()
            {
                if (index >= text.Length) throw new FormatException("Unexpected end of the JSON.");
                return text[index++];
            }

            void Expect(string word)
            {
                if (string.CompareOrdinal(text, index, word, 0, word.Length) != 0)
                    throw new FormatException($"Expected `{word}` at {index}.");
                index += word.Length;
            }

            public object ReadValue()
            {
                SkipWhitespace();
                if (AtEnd) throw new FormatException("Unexpected end of the JSON.");
                switch (text[index])
                {
                    case '{': return ReadObject();
                    case '[': return ReadArray();
                    case '"': return ReadString();
                    case 't': Expect("true"); return true;
                    case 'f': Expect("false"); return false;
                    case 'n': Expect("null"); return null;
                    default: return ReadNumber();
                }
            }

            Dictionary<string, object> ReadObject()
            {
                var result = new Dictionary<string, object>();
                Next();
                SkipWhitespace();
                if (!AtEnd && text[index] == '}') { index++; return result; }
                while (true)
                {
                    SkipWhitespace();
                    var key = ReadString();
                    SkipWhitespace();
                    if (Next() != ':') throw new FormatException($"Expected `:` at {index - 1}.");
                    result[key] = ReadValue();
                    SkipWhitespace();
                    var c = Next();
                    if (c == '}') return result;
                    if (c != ',') throw new FormatException($"Expected `,` or `}}` at {index - 1}.");
                }
            }

            List<object> ReadArray()
            {
                var result = new List<object>();
                Next();
                SkipWhitespace();
                if (!AtEnd && text[index] == ']') { index++; return result; }
                while (true)
                {
                    result.Add(ReadValue());
                    SkipWhitespace();
                    var c = Next();
                    if (c == ']') return result;
                    if (c != ',') throw new FormatException($"Expected `,` or `]` at {index - 1}.");
                }
            }

            string ReadString()
            {
                if (Next() != '"') throw new FormatException($"Expected a string at {index - 1}.");
                var builder = new StringBuilder();
                while (true)
                {
                    var c = Next();
                    if (c == '"') return builder.ToString();
                    if (c != '\\') { builder.Append(c); continue; }
                    var escaped = Next();
                    switch (escaped)
                    {
                        case '"': builder.Append('"'); break;
                        case '\\': builder.Append('\\'); break;
                        case '/': builder.Append('/'); break;
                        case 'b': builder.Append('\b'); break;
                        case 'f': builder.Append('\f'); break;
                        case 'n': builder.Append('\n'); break;
                        case 'r': builder.Append('\r'); break;
                        case 't': builder.Append('\t'); break;
                        case 'u':
                            if (index + 4 > text.Length) throw new FormatException("Unexpected end of the JSON.");
                            builder.Append((char)int.Parse(text.Substring(index, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture));
                            index += 4;
                            break;
                        default: throw new FormatException($"Unknown escape `\\{escaped}` at {index - 1}.");
                    }
                }
            }

            object ReadNumber()
            {
                var start = index;
                while (index < text.Length && "+-0123456789.eE".IndexOf(text[index]) >= 0) index++;
                var number = text.Substring(start, index - start);
                if (number.Length == 0) throw new FormatException($"Unexpected `{text[start]}` at {start}.");
                if (number.IndexOfAny(new[] { '.', 'e', 'E' }) < 0 &&
                    long.TryParse(number, NumberStyles.Integer, CultureInfo.InvariantCulture, out var whole))
                    return whole;
                return double.Parse(number, NumberStyles.Float, CultureInfo.InvariantCulture);
            }
        }
    }
}
