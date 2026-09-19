/** @jsxImportSource @metamask/snaps-sdk */
import { SeverityLevel } from '@metamask/snaps-sdk';
import { Box, Heading, Text, Divider, Copyable } from '@metamask/snaps-sdk/jsx';

import { API_BASE, analyze, display } from './api.mjs';

const TOP_FACTORS = 3;

function card(children, severity) {
  return {
    severity,
    content: (
      <Box>
        <Heading>Multipli Risk</Heading>
        {children}
      </Box>
    ),
  };
}

export const onTransaction = async ({ transaction }) => {
  const to = transaction.to;

  if (!to) {
    return card(<Text>Contract deployment — no counterparty address to analyze.</Text>);
  }

  let result;
  try {
    result = await analyze(to.toLowerCase());
  } catch (error) {
    return card(
      <Box>
        <Text>Could not reach the risk API at {API_BASE}</Text>
        <Text>{String(error.message)}</Text>
      </Box>,
    );
  }

  if (!result.features) {
    return card(
      <Box>
        <Copyable value={to} />
        <Text>{result.detail}</Text>
      </Box>,
    );
  }

  const { kind, features, verdict } = result;
  const drivers = verdict
    ? verdict.factors.filter((f) => f.direction === 'raises risk').slice(0, TOP_FACTORS)
    : [];

  return card(
    <Box>
      {verdict ? (
        <Text>
          **{`${verdict.band} — riskier than ${verdict.risk_score}% of legitimate wallets`}**
        </Text>
      ) : (
        <Text>Analyzed as {kind}; no model verdict for contracts yet.</Text>
      )}
      <Copyable value={to} />
      {drivers.length > 0 ? <Divider /> : null}
      {drivers.map((f) => (
        <Text>{`• ${f.text}`}</Text>
      ))}
      <Divider />
      {Object.entries(features)
        .filter(([key]) => key !== 'address')
        .map(([key, value]) => (
          <Text>{`${key}: ${display(value)}`}</Text>
        ))}
    </Box>,
    verdict && verdict.band !== 'ALLOW' ? SeverityLevel.Critical : undefined,
  );
};
