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
import { FaLock, FaUnlock, FaShieldAlt } from "react-icons/fa";
import { callable, toaster } from "@decky/api";

const getSectorInfo = callable<[uid?: string], Array<{sector: number; first_block: number; trailer_block: number; locked: boolean; readable: boolean; writable: boolean}>>("get_sector_info");
const lockSector = callable<[uid: string, sector: number, key_a: string, key_b: string], boolean>("lock_sector");

interface SectorInfo {
  sector: number;
  first_block: number;
  trailer_block: number;
  locked: boolean;
  readable: boolean;
  writable: boolean;
}

const LockSectorModal: FC<{
  uid: string;
  sector: number;
  onSuccess: () => void;
}> = ({ uid, sector, onSuccess }) => {
  const [keyA, setKeyA] = useState("FFFFFFFFFFFF");
  const [keyB, setKeyB] = useState("FFFFFFFFFFFF");
  const [loading, setLoading] = useState(false);

  const handleLock = async () => {
    if (keyA.length !== 12 || keyB.length !== 12) {
      toaster.toast({ title: "Error", body: "Keys must be 12 hex characters", critical: true });
      return;
    }

    setLoading(true);
    try {
      const success = await lockSector(uid, sector, keyA.toUpperCase(), keyB.toUpperCase());
      if (success) {
        toaster.toast({ title: "Success", body: `Sector ${sector} locked` });
        onSuccess();
        closeModal();
      } else {
        toaster.toast({ title: "Error", body: "Failed to lock sector", critical: true });
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
        <h2>Lock Sector {sector}</h2>
        <div style={{ fontSize: "0.9em", opacity: 0.7 }}>
          Warning: Locking a sector will make it read-only. This cannot be easily undone.
        </div>
        <TextField
          label="Key A (12 hex chars)"
          value={keyA}
          onChange={(e) => setKeyA(e.target.value)}
          placeholder="FFFFFFFFFFFF"
        />
        <TextField
          label="Key B (12 hex chars)"
          value={keyB}
          onChange={(e) => setKeyB(e.target.value)}
          placeholder="FFFFFFFFFFFF"
        />
        <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
          <ButtonItem onClick={() => closeModal()} disabled={loading}>
            Cancel
          </ButtonItem>
          <ButtonItem onClick={handleLock} disabled={loading}>
            {loading ? "Locking..." : "Lock Sector"}
          </ButtonItem>
        </div>
      </div>
    </ModalRoot>
  );
};

export const SectorManagementPanel: FC<{ tagUid?: string }> = ({ tagUid }) => {
  const [sectors, setSectors] = useState<SectorInfo[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (tagUid) {
      loadSectors();
    }
  }, [tagUid]);

  const loadSectors = async () => {
    if (!tagUid) return;
    
    setLoading(true);
    try {
      const info = await getSectorInfo(tagUid);
      setSectors(info);
    } catch (e) {
      console.error("Failed to load sectors:", e);
      toaster.toast({ title: "Error", body: "Failed to load sector info", critical: true });
    } finally {
      setLoading(false);
    }
  };

  const handleLockSector = (sector: number) => {
    if (!tagUid) return;
    
    showModal(
      <LockSectorModal
        uid={tagUid}
        sector={sector}
        onSuccess={() => loadSectors()}
      />
    );
  };

  if (!tagUid) {
    return (
      <PanelSection title="Sector Management">
        <PanelSectionRow>
          <div style={{ fontSize: "0.9em", opacity: 0.7 }}>
            No Mifare Classic tag detected
          </div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  return (
    <PanelSection title="Sector Management">
      <PanelSectionRow>
        <div style={{ fontSize: "0.9em", opacity: 0.7, marginBottom: "8px" }}>
          Manage sector lock status for Mifare Classic tags
        </div>
      </PanelSectionRow>

      {loading ? (
        <PanelSectionRow>
          <div style={{ fontSize: "0.9em", opacity: 0.7 }}>Loading sectors...</div>
        </PanelSectionRow>
      ) : sectors.length === 0 ? (
        <PanelSectionRow>
          <div style={{ fontSize: "0.9em", opacity: 0.7 }}>No sector info available</div>
        </PanelSectionRow>
      ) : (
        <>
          {sectors.map((sector) => (
            <PanelSectionRow key={sector.sector}>
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
                {sector.locked ? (
                  <FaLock size={14} color="#ff6b6b" />
                ) : sector.writable ? (
                  <FaUnlock size={14} color="#51cf66" />
                ) : (
                  <FaShieldAlt size={14} color="#ffd43b" />
                )}
                <div style={{ flex: 1, fontSize: "0.85em" }}>
                  <div style={{ fontWeight: "bold" }}>Sector {sector.sector}</div>
                  <div style={{ opacity: 0.6, fontSize: "0.9em" }}>
                    Blocks {sector.first_block}-{sector.trailer_block} • 
                    {sector.locked ? " Locked" : sector.writable ? " Writable" : " Read-only"}
                  </div>
                </div>
                {!sector.locked && sector.writable && (
                  <ButtonItem
                    onClick={() => handleLockSector(sector.sector)}
                    style={{ padding: "4px 8px", fontSize: "0.8em" }}
                  >
                    Lock
                  </ButtonItem>
                )}
              </div>
            </PanelSectionRow>
          ))}
          <PanelSectionRow>
            <ButtonItem onClick={loadSectors} layout="below">
              Refresh Sectors
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};
