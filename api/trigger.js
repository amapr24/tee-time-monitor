// api/trigger.js
// Vercel serverless function -- triggers the GitHub Actions workflow
// The GITHUB_PAT environment variable is set in Vercel's dashboard (never in code)

export default async function handler(req, res) {
  // Only allow POST
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  const token = process.env.GITHUB_PAT;
  if (!token) {
    return res.status(500).json({ error: "GITHUB_PAT not configured" });
  }

  try {
    const response = await fetch(
      "https://api.github.com/repos/amapr24/tee-time-monitor/actions/workflows/tee-time-monitor.yml/dispatches",
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Accept":        "application/vnd.github.v3+json",
          "Content-Type":  "application/json",
        },
        body: JSON.stringify({ ref: "main" }),
      }
    );

    if (response.status === 204) {
      return res.status(200).json({ ok: true, message: "Workflow triggered!" });
    } else {
      const text = await response.text();
      return res.status(response.status).json({ error: text });
    }
  } catch (err) {
    return res.status(500).json({ error: err.message });
  }
}
