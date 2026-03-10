import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  TextField,
  ModalRoot,
  showModal,
  closeModal,
} from "@decky/ui";
import { FC, useState, useEffect } from "react";
import { FaKey, FaTrash, FaPlus } from "react-icons/fa";
import { callable, toaster } from "@decky/api";

const setTagKey = callable<[uid: string, key_a: string, key_b: string], boolean>("set_tag_key");
const getTagKey = callable<[uid: string], { key_a?: string; key_b?: string }>("get_tag_key");
const listTagKeys = callable<[], string[]>("list_tag_keys");

interface KeyEntry {
  uid: string;
  key_a: string;
  key_b: string;
}

const KeyEditModal: FC<{
  uid?: string;
  onSave: (uid: string, key_a: string, key_b: string) => void;
}> = ({ uid, onSave }) => {
  const [formUid, setFormUid] = useState(uid || "");
  const [keyA, setKeyA] = useState("");
  const [keyB, setKeyB] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (uid) {
      setFormUid(uid);
      loadKeys(uid);
    }
  }, [uid]);

  const loadKeys = async (tagUid: string) => {
    try {
      const keys = await getTagKey(tagUid);
      if (keys.key_a) setKeyA(keys.key_a);
      if (keys.key_b) setKeyB(keys.key_b);
    } catch (e) {
      console.error("Failed to load keys:", e);
    }
  };

  const handleSave = async () => {
    if (!formUid.trim()) {
      toaster.toast({ title: "Error", body: "UID cannot be empty", critical: true });
      return;
    }
    if (!keyA.trim() || !keyB.trim()) {
      toaster.toast({ title: "Error", body: "Both keys must be provided", critical: true });
      return;
    }
    if (keyA.length !== 12 || keyB.length !== 12) {
      toaster.toast({ title: "Error", body: "Keys must be 12 hex characters (6 bytes)", critical: true });
      return;
    }

    setLoading(true);
    try {
      const success = await setTagKey(formUid.toUpperCase(), keyA.toUpperCase(), keyB.toUpperCase());
      if (success) {
        toaster.toast({ title: "Success", body: `Keys saved for ${formUid}` });
        onSave(formUid.toUpperCase(), keyA.toUpperCase(), keyB.toUpperCase());
        closeModal();
      } else {
        toaster.toast({ title: "Error", body: "Failed to save keys", critical: true });
      }
    } catch (e) {
      toaster.toast({ title: "Error", body: String(e), critical: true });
    } finally {
      setLoading(false);
    }
  };

  return (
    <ModalRoot>
      <div style={{ padding: "20px", display: "flex", flexDirection: "column", gap: "12px" }}>
        <h2>{uid ? "Edit Key" : "Add New Key"}</h2>
        <TextField
          label="Tag UID (hex)"
          value={formUid}
          onChange={(e) => setFormUid(e.target.value)}
          disabled={!!uid}
          placeholder="e.g., DEADBEEFCAFE"
        />
        <TextField
          label="Key A (12 hex chars)"
          value={keyA}
          onChange={(e) => setKeyA(e.target.value)}
          placeholder="e.g., FFFFFFFFFFFF"
        />
        <TextField
          label="Key B (12 hex chars)"
          value={keyB}
          onChange={(e) => setKeyB(e.target.value)}
          placeholder="e.g., D3F7D3F7D3F7"
        />
        <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
          <ButtonItem onClick={() => closeModal()} disabled={loading}>
            Cancel
          </ButtonItem>
          <ButtonItem onClick={handleSave} disabled={loading}>
            {loading ? "Saving..." : "Save"}
          </ButtonItem>
        </div>
      </div>
    </ModalRoot>
  );
};

export const KeyManagementPanel: FC = () => {
  const [keys, setKeys] = useState<KeyEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadKeys();
  }, []);

  const loadKeys = async () => {
    setLoading(true);
    try {
      const uids = await listTagKeys();
      const entries: KeyEntry[] = [];
      for (const uid of uids) {
        const keyData = await getTagKey(uid);
        if (keyData.key_a && keyData.key_b) {
          entries.push({
            uid,
            key_a: keyData.key_a,
            key_b: keyData.key_b,
          });
        }
      }
      setKeys(entries);
    } catch (e) {
      console.error("Failed to load keys:", e);
      toaster.toast({ title: "Error", body: "Failed to load keys", critical: true });
    } finally {
      setLoading(false);
    }
  };

  const handleAddKey = () => {
    showModal(
      <KeyEditModal
        onSave={() => loadKeys()}
      />
    );
  };

  const handleEditKey = (uid: string) => {
    showModal(
      <KeyEditModal
        uid={uid}
        onSave={() => loadKeys()}
      />
    );
  };

  return (
    <PanelSection title="Custom Keys">
      <PanelSectionRow>
        <div style={{ fontSize: "0.9em", opacity: 0.7, marginBottom: "8px" }}>
          Manage Mifare Classic authentication keys per tag UID
        </div>
      </PanelSectionRow>

      {loading ? (
        <PanelSectionRow>
          <div style={{ fontSize: "0.9em", opacity: 0.7 }}>Loading...</div>
        </PanelSectionRow>
      ) : keys.length === 0 ? (
        <PanelSectionRow>
          <div style={{ fontSize: "0.9em", opacity: 0.7 }}>No custom keys stored</div>
        </PanelSectionRow>
      ) : (
        keys.map((entry) => (
          <PanelSectionRow key={entry.uid}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "8px",
                padding: "8px",
                backgroundColor: "rgba(255,255,255,0.05)",
                borderRadius: "4px",
                flex: 1,
              }}
            >
              <FaKey size={14} />
              <div style={{ flex: 1, fontSize: "0.85em", fontFamily: "monospace" }}>
                <div>{entry.uid}</div>
                <div style={{ opacity: 0.6, fontSize: "0.8em" }}>
                  A: {entry.key_a.substring(0, 6)}... B: {entry.key_b.substring(0, 6)}...
                </div>
              </div>
              <ButtonItem
                onClick={() => handleEditKey(entry.uid)}
                style={{ padding: "4px 8px", fontSize: "0.8em" }}
              >
                Edit
              </ButtonItem>
              <ButtonItem
                onClick={() => {
                  setKeys(keys.filter((k) => k.uid !== entry.uid));
                  toaster.toast({ title: "Deleted", body: `Keys for ${entry.uid} removed` });
                }}
                style={{ padding: "4px 8px", fontSize: "0.8em" }}
              >
                <FaTrash size={12} />
              </ButtonItem>
            </div>
          </PanelSectionRow>
        ))
      )}

      <PanelSectionRow>
        <ButtonItem onClick={handleAddKey} layout="below">
          <FaPlus size={14} style={{ marginRight: "8px" }} />
          Add Custom Key
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};
