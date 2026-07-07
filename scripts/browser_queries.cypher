// ==========================================================================
// CleanPlay — Neo4j Browser visualization queries
// Paste either block into the Neo4j Browser query bar and run.
// ==========================================================================

// --------------------------------------------------------------------------
// (a) THE WHOLE GRAPH — every Account / Device / IP and their relationships.
//     Good for the "here is all the telemetry" overview shot.
// --------------------------------------------------------------------------
MATCH (n)
WHERE n:Account OR n:Device OR n:IP
OPTIONAL MATCH p = (n)-[]->()
RETURN n, p;


// --------------------------------------------------------------------------
// (b) THE RING SUBGRAPH — just the farmer, the two mules (shared device D-42),
//     the three buyers, plus their devices, shared IPs and gold transfers.
//     Ring accounts are the ones whose id starts with 'A-'.
// --------------------------------------------------------------------------
MATCH (a:Account)
WHERE a.id STARTS WITH 'A-'
OPTIONAL MATCH (a)-[t:TRANSFERRED_TO]->(b:Account)
OPTIONAL MATCH (a)-[l:LOGGED_IN_FROM]->(d:Device)
OPTIONAL MATCH (d)-[u:USES_IP]->(ip:IP)
RETURN a, t, b, l, d, u, ip;
