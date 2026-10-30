/**
 * Module pur de détection multi-instance pour la commande /runs.
 * ZÉRO import, aucune dépendance, base64 maison.
 */

export type Instance = {
  id: string;
  base: string;
  auth: string | null;
};

/**
 * Table d'encodage Base64 standard (RFC 4648).
 */
const BASE64_ALPHABET =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/**
 * Encode une chaîne UTF-8 en Base64 (alphabet standard, padding '=').
 * Ne lève jamais d'exception.
 */
function base64Encode(str: string): string {
  // Convertir la chaîne en octets UTF-8
  const bytes: number[] = [];
  for (let i = 0; i < str.length; i++) {
    const code = str.charCodeAt(i);
    if (code < 0x80) {
      bytes.push(code);
    } else if (code < 0x800) {
      bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
    } else if (code < 0xd800 || code >= 0xe000) {
      bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    } else {
      // Paires de substituts (surrogates) - encodées comme caractère de remplacement
      i++;
      bytes.push(0xef, 0xbf, 0xbd);
    }
  }

  // Encoder en Base64
  let out = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const b1 = bytes[i];
    const b2 = i + 1 < bytes.length ? bytes[i + 1] : 0;
    const b3 = i + 2 < bytes.length ? bytes[i + 2] : 0;

    const triple = (b1 << 16) | (b2 << 8) | b3;

    out += BASE64_ALPHABET[(triple >> 18) & 0x3f];
    out += BASE64_ALPHABET[(triple >> 12) & 0x3f];
    out += i + 1 < bytes.length ? BASE64_ALPHABET[(triple >> 6) & 0x3f] : "=";
    out += i + 2 < bytes.length ? BASE64_ALPHABET[triple & 0x3f] : "=";
  }

  return out;
}

/**
 * Construit l'en-tête Authorization Basic.
 * @param user - nom d'utilisateur (ex: "opencode")
 * @param password - mot de passe (jamais retourné en clair)
 * @returns "Basic " + base64(user + ":" + password)
 */
export function buildAuth(user: string, password: string): string {
  const credentials = `${user}:${password}`;
  return `Basic ${base64Encode(credentials)}`;
}

/**
 * Parse le fichier service.json du daemon.
 * @param raw - contenu brut du fichier (string ou null)
 * @param fallbackBase - base URL de secours si absente du JSON
 * @returns Instance | null (jamais d'exception)
 */
export function parseService(
  raw: string | null,
  fallbackBase?: string
): Instance | null {
  if (raw === null || raw === undefined) return null;

  let parsed: any;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }

  if (!parsed || typeof parsed !== "object") return null;

  const url = typeof parsed.url === "string" ? parsed.url.trim() : "";
  const password = typeof parsed.password === "string" ? parsed.password : "";

  if (!url) return null;

  // Retirer le slash final s'il y en a un
  const base = url.endsWith("/") ? url.slice(0, -1) : url;
  const auth = password ? buildAuth("opencode", password) : null;

  return {
    id: "service",
    base,
    auth,
  };
}

/**
 * Fusionne les maps `active` de plusieurs instances.
 * L'instance courante (ownID) a la priorité, puis les `others` dans l'ordre.
 * @returns { owner: Record<sessionID, instanceID>, running: sessionID[] }
 *          running = ordre d'apparition sans doublon
 */
export function mergeActive(input: {
  ownID: string;
  own: Record<string, any>;
  others: Array<{ id: string; active: Record<string, any> }>;
}): { owner: Record<string, string>; running: string[] } {
  const ownID = typeof input.ownID === "string" ? input.ownID : "own";
  const own = input.own && typeof input.own === "object" ? input.own : {};
  const others = Array.isArray(input.others) ? input.others : [];

  const owner: Record<string, string> = {};
  const running: string[] = [];
  const seen = new Set<string>();

  // 1. Instance courante (priorité)
  for (const [sessionID, info] of Object.entries(own)) {
    if (info && typeof info === "object" && info.type === "running") {
      if (!owner[sessionID]) {
        owner[sessionID] = ownID;
      }
      if (!seen.has(sessionID)) {
        seen.add(sessionID);
        running.push(sessionID);
      }
    }
  }

  // 2. Autres instances (dans l'ordre)
  for (const other of others) {
    if (!other || typeof other !== "object") continue;
    const id = typeof other.id === "string" ? other.id : "";
    const active = other.active && typeof other.active === "object" ? other.active : {};
    if (!id) continue;

    for (const [sessionID, info] of Object.entries(active)) {
      if (info && typeof info === "object" && info.type === "running") {
        if (!owner[sessionID]) {
          owner[sessionID] = id;
        }
        if (!seen.has(sessionID)) {
          seen.add(sessionID);
          running.push(sessionID);
        }
      }
    }
  }

  return { owner, running };
}

/**
 * Détermine l'instance propriétaire d'une session.
 * @param sessionID - ID de la session
 * @param owner - map sessionID → instanceID (sortie de mergeActive)
 * @param ownID - ID de l'instance courante (fallback)
 * @returns instanceID (owner[sessionID] ou ownID)
 */
export function instanceFor(
  sessionID: string,
  owner: Record<string, string>,
  ownID: string
): string {
  return owner[sessionID] ?? ownID;
}