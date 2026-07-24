import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Content-Type": "application/json; charset=utf-8",
};

function jsonResponse(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: corsHeaders });
}

function decodeBase64Url(value: string): Uint8Array {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function getServiceKey(): string {
  const legacy = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
  if (legacy) return legacy;
  try {
    const keys = JSON.parse(Deno.env.get("SUPABASE_SECRET_KEYS") || "{}");
    return String(keys.default || Object.values(keys)[0] || "");
  } catch (_) {
    return "";
  }
}

async function verifyToken(secret: string, token: string) {
  const [encoded, signature] = token.split(".");
  if (!encoded || !signature) throw new Error("invalid_token");
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const valid = await crypto.subtle.verify(
    "HMAC",
    key,
    decodeBase64Url(signature),
    new TextEncoder().encode(encoded),
  );
  if (!valid) throw new Error("invalid_signature");
  const payload = JSON.parse(new TextDecoder().decode(decodeBase64Url(encoded)));
  if (!["helpful", "irrelevant", "deep_dive"].includes(payload.a)) {
    throw new Error("invalid_action");
  }
  if (!payload.exp || Number(payload.exp) * 1000 < Date.now()) {
    throw new Error("expired");
  }
  return payload as { u: string; t: string; i: string; a: string; exp: number };
}

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (request.method !== "POST") return jsonResponse({ error: "method_not_allowed" }, 405);

  try {
    const secret = Deno.env.get("FEEDBACK_SIGNING_SECRET") || "";
    const supabaseUrl = Deno.env.get("SUPABASE_URL") || "";
    const serviceKey = getServiceKey();
    if (!secret || !supabaseUrl || !serviceKey) {
      return jsonResponse({ error: "server_not_configured" }, 500);
    }
    const body = await request.json();
    const payload = await verifyToken(secret, String(body.token || ""));
    const supabase = createClient(supabaseUrl, serviceKey, {
      auth: { persistSession: false },
    });

    const { data: track, error: trackError } = await supabase
      .from("tracks")
      .select("id")
      .eq("id", payload.t)
      .eq("beta_user_id", payload.u)
      .maybeSingle();
    if (trackError || !track) return jsonResponse({ error: "invalid_scope" }, 403);

    const { error: feedbackError } = await supabase.from("feedback").upsert(
      {
        beta_user_id: payload.u,
        track_id: payload.t,
        item_id: payload.i,
        value: payload.a,
        updated_at: new Date().toISOString(),
      },
      { onConflict: "beta_user_id,track_id,item_id" },
    );
    if (feedbackError) throw feedbackError;

    const { error: eventError } = await supabase.from("analytics_events").insert({
      beta_user_id: payload.u,
      track_id: payload.t,
      item_id: payload.i,
      event_name: `feedback_${payload.a}`,
      properties: { source: "digest_email" },
    });
    if (eventError) throw eventError;

    const messages: Record<string, string> = {
      helpful: "已记录：这条推荐有用",
      irrelevant: "已记录：这条推荐不相关",
      deep_dive: "已记录：你希望继续深挖",
    };
    return jsonResponse({ ok: true, action: payload.a, message: messages[payload.a] });
  } catch (error) {
    console.error(error);
    return jsonResponse({ error: "invalid_or_expired_feedback" }, 400);
  }
});
