import { NextResponse, type NextRequest } from "next/server";

// Proteção de rota no edge: sem cookie de sessão, nem tenta abrir o dashboard, o chat ou a administração.
// A validação real do token é sempre feita pela API.
export function middleware(request: NextRequest) {
  const hasSession = request.cookies.has("ca_access") || request.cookies.has("ca_session");
  if (!hasSession) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    url.search = `?next=${encodeURIComponent(request.nextUrl.pathname)}`;
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = { matcher: ["/dashboard/:path*", "/chat/:path*", "/admin/:path*"] };
