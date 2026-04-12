import { NextRequest, NextResponse } from "next/server";

const API_URL = process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://query-api:8000";

async function proxy(req: NextRequest) {
  const path = req.nextUrl.pathname.replace(/^\/api/, "");
  const target = `${API_URL}${path}${req.nextUrl.search}`;

  const headers = new Headers(req.headers);
  headers.delete("host");

  const res = await fetch(target, {
    method: req.method,
    headers,
    body: req.method !== "GET" && req.method !== "HEAD" ? await req.blob() : undefined,
  });

  const responseHeaders = new Headers(res.headers);
  return new NextResponse(res.body, {
    status: res.status,
    headers: responseHeaders,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
export const PATCH = proxy;
