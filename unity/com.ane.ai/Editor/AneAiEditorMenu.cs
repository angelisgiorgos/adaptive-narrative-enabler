// Editor menu: import an ANE AI export (or a runtime zip) into StreamingAssets/ANE-AI, and open the
// folder where the ANE AI keeps profiles and save games.
using System;
using System.IO;
using UnityEditor;
using UnityEngine;

namespace Ane.AI.Editor
{
    public static class AneAiEditorMenu
    {
        static string Target => Path.Combine(Application.dataPath, "StreamingAssets", "ANE-AI");

        [MenuItem("Tools/ANE AI/Import ANE AI export (.zip)…")]
        public static void ImportExport()
        {
            var zip = EditorUtility.OpenFilePanel("ANE AI export or runtime (.zip)", "", "zip");
            if (string.IsNullOrEmpty(zip)) return;
            try
            {
                var report = AneAiExportImport.Import(zip, Target);
                AssetDatabase.Refresh();
                EditorUtility.DisplayDialog("ANE AI", report, "OK");
            }
            catch (Exception exception)
            {
                EditorUtility.DisplayDialog("ANE AI", exception.Message, "OK");
            }
        }

        [MenuItem("Tools/ANE AI/Open the ANE AI data folder")]
        public static void OpenDataFolder()
        {
            var folder = Path.Combine(Application.persistentDataPath, "ANE-AI");
            Directory.CreateDirectory(folder);
            EditorUtility.RevealInFinder(folder);
        }

        [MenuItem("Tools/ANE AI/Add the ANE AI to the scene")]
        public static void AddToScene()
        {
            var existing = UnityEngine.Object.FindObjectOfType<AneAiBehaviour>();
            if (existing != null)
            {
                Selection.activeObject = existing.gameObject;
                return;
            }
            var created = new GameObject("ANE AI");
            created.AddComponent<AneAiBehaviour>();
            Undo.RegisterCreatedObjectUndo(created, "Add the ANE AI");
            Selection.activeObject = created;
        }
    }
}
