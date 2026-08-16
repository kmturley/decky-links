import { ReactNode } from "react";
import {
  FaCircle,
  FaMicrochip,
  FaHdd,
  FaCamera,
  FaWifi,
  FaPlug,
  FaFolderOpen,
} from "react-icons/fa";
import { SourceType } from "../shared";

/** Icon for a trigger source. Shared by the QAM panel and the game-page modal
 *  so the same source never appears as two different symbols. */
export function sourceIcon(sourceType: string): ReactNode {
  switch (sourceType) {
    case SourceType.NFC: return <FaMicrochip />;
    case SourceType.STORAGE: return <FaHdd />;
    case SourceType.CAMERA: return <FaCamera />;
    case SourceType.MQTT: return <FaWifi />;
    case SourceType.SERIAL: return <FaPlug />;
    case SourceType.FILE_WATCH: return <FaFolderOpen />;
    default: return <FaCircle />;
  }
}

/** What the user physically presents to this source, e.g. "tap a tag". */
export function presentMediaVerb(sourceType: string): string {
  switch (sourceType) {
    case SourceType.NFC: return "tap a tag";
    case SourceType.STORAGE: return "insert a disk";
    case SourceType.CAMERA: return "show a QR code";
    case SourceType.SERIAL: return "send a code";
    default: return "present media";
  }
}

/** Human-readable device name for a source. */
export function sourceLabel(sourceType: string): string {
  switch (sourceType) {
    case SourceType.NFC: return "NFC reader";
    case SourceType.STORAGE: return "Disk drive";
    case SourceType.CAMERA: return "Camera";
    case SourceType.MQTT: return "MQTT";
    case SourceType.SERIAL: return "Serial";
    case SourceType.FILE_WATCH: return "File watch";
    default: return sourceType;
  }
}

/** Join phrases the way a person would: "a, b or c". */
export function joinWithOr(parts: string[]): string {
  if (parts.length === 0) return "";
  if (parts.length === 1) return parts[0];
  return `${parts.slice(0, -1).join(", ")} or ${parts[parts.length - 1]}`;
}
