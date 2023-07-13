// main.js

// Function to handle form submission
function handleSubmit(event) {
    event.preventDefault();
    
    // Get form input values
    const host = document.getElementById('host').value;
    const username = document.getElementById('username').value;
    const password = document.getElementById('password').value;
    
    // Create a JSON object with the form data
    const formData = {
      host: host,
      username: username,
      password: password
    };
    
    // Send an AJAX request to the server
    fetch('/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(formData)
    })
    .then(response => response.json())
    .then(data => {
      // Display the response from the server
      const resultDiv = document.getElementById('result');
      resultDiv.textContent = data.message;
    })
    .catch(error => {
      console.error('Error:', error);
    });
  }
  
  // Add event listener to the form submit button
  const form = document.getElementById('loginForm');
  form.addEventListener('submit', handleSubmit);
  