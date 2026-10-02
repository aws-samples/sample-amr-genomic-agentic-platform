import React from 'react';
import ReactDOM from 'react-dom/client';
import { Amplify } from 'aws-amplify';
import App from './App';
import { loadRuntimeConfig } from './config';
import './styles/global.css';

// Bootstrap: load runtime config, configure Amplify, then render. Doing this
// before render guarantees Amplify has real Cognito values (fetched at runtime,
// not baked into the bundle) and avoids a blank page when config is wrong.
async function bootstrap(): Promise<void> {
  const config = await loadRuntimeConfig();

  // Auth is handled in-app via Amplify SRP username/password (see pages/Login),
  // so only the user pool + client are needed here — no Hosted UI OAuth config.
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: config.userPoolId,
        userPoolClientId: config.userPoolClientId,
      },
    },
  });

  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

bootstrap().catch((err) => {
  // Render a minimal, honest error instead of a silent blank page.
  console.error('Application bootstrap failed:', err);
  const root = document.getElementById('root');
  if (root) {
    // Build the error notice with DOM APIs rather than innerHTML so no markup
    // is parsed from a string.
    const container = document.createElement('div');
    container.style.fontFamily = 'system-ui';
    container.style.padding = '2rem';
    container.style.maxWidth = '40rem';
    container.style.margin = '0 auto';

    const heading = document.createElement('h1');
    heading.textContent = 'Configuration error';

    const message = document.createElement('p');
    message.append(
      'The application could not load its runtime configuration (',
    );
    const code = document.createElement('code');
    code.textContent = '/runtime-config.json';
    message.append(code);
    message.append(
      '). Check that the frontend stack deployed successfully and the file is present.',
    );

    container.append(heading, message);
    root.replaceChildren(container);
  }
});
