import FilePage from "@/components/file-page";

export default async function Page({ params }: { params: Promise<{ session: string; path: string[] }> }) {
  const { session, path } = await params;
  const filePath = path.join("/");
  return <FilePage key={`${session}/${filePath}`} sessionId={session} path={filePath} />;
}
