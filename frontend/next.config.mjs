/**
 * O frontend fala com a API sempre pela mesma origem (/api/* e /health),
 * reencaminhado para API_URL. Assim os cookies de sessão httpOnly funcionam
 * sem CORS, tanto em desenvolvimento como na Vercel.
 */
const apiUrl = (process.env.API_URL || "http://localhost:8000").replace(/\/$/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiUrl}/api/:path*` },
      { source: "/backend-health", destination: `${apiUrl}/health` },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
