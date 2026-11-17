// Stand-ins for the UnityEngine / UnityEditor members the package uses: enough to compile it
// outside Unity (CI). Never shipped.
using System;

namespace UnityEngine
{
    public class Object
    {
        public static T FindObjectOfType<T>() where T : Object => null;
        public static void DontDestroyOnLoad(Object target) { }
    }

    public class Component : Object { public GameObject gameObject => null; }
    public class Behaviour : Component { }
    public class MonoBehaviour : Behaviour { }

    public sealed class GameObject : Object
    {
        public GameObject(string name) { }
        public T AddComponent<T>() where T : Component => null;
    }

    public static class Debug
    {
        public static void Log(object message) { }
        public static void LogWarning(object message) { }
        public static void LogError(object message) { }
    }

    public static class Application
    {
        public static string streamingAssetsPath => "";
        public static string persistentDataPath => "";
        public static string dataPath => "";
    }

    public static class Screen { public static int width => 800; public static int height => 600; }
    public struct Rect { public Rect(float x, float y, float width, float height) { } }
    public struct Vector2 { }

    public sealed class GUILayoutOption { }

    public static class GUILayout
    {
        public static void Label(string text, params GUILayoutOption[] options) { }
        public static bool Button(string text, params GUILayoutOption[] options) => false;
        public static void BeginArea(Rect area) { }
        public static void EndArea() { }
        public static Vector2 BeginScrollView(Vector2 position, params GUILayoutOption[] options) => position;
        public static void EndScrollView() { }
        public static void BeginHorizontal(params GUILayoutOption[] options) { }
        public static void EndHorizontal() { }
        public static GUILayoutOption Height(float height) => null;
    }

    [AttributeUsage(AttributeTargets.Field)] public sealed class TooltipAttribute : Attribute { public TooltipAttribute(string tooltip) { } }
    [AttributeUsage(AttributeTargets.Class)] public sealed class DisallowMultipleComponent : Attribute { }
    [AttributeUsage(AttributeTargets.Class)] public sealed class AddComponentMenu : Attribute { public AddComponentMenu(string menu) { } }
}

namespace UnityEditor
{
    [AttributeUsage(AttributeTargets.Method)] public sealed class MenuItem : Attribute { public MenuItem(string path) { } }

    public static class EditorUtility
    {
        public static string OpenFilePanel(string title, string directory, string extension) => "";
        public static bool DisplayDialog(string title, string message, string ok) => true;
        public static void RevealInFinder(string path) { }
    }

    public static class AssetDatabase { public static void Refresh() { } }
    public static class Selection { public static UnityEngine.Object activeObject { get; set; } }
    public static class Undo { public static void RegisterCreatedObjectUndo(UnityEngine.Object created, string name) { } }
}
