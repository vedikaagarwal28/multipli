/** @jsxImportSource @metamask/snaps-sdk */
import { Box, Heading, Text, Divider, Copyable } from '@metamask/snaps-sdk/jsx';

import { API_BASE, analyze, display } from './api.mjs';

function card(children) {
  return {
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

  return card(
    <Box>
      <Text>Counterparty analyzed as {result.kind}</Text>
      <Copyable value={to} />
      <Divider />
      {Object.entries(result.features)
        .filter(([key]) => key !== 'address')
        .map(([key, value]) => (
          <Text>{`${key}: ${display(value)}`}</Text>
        ))}
    </Box>,
  );
};
