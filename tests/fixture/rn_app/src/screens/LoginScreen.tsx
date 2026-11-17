import React, { useState } from 'react';
import { View, Text, TextInput, TouchableOpacity } from 'react-native';
import api from '../api';

export default function LoginScreen({ navigation }) {
  const [email, setEmail] = useState('');
  const onSubmit = async () => {
    await api.post('/auth/login', { email });
    navigation.replace('Home');
  };
  return (
    <View>
      <Text>Sign in to LifeSADX</Text>
      <TextInput placeholder="Email" keyboardType="email-address" value={email} onChangeText={setEmail} testID="email-input" />
      <TextInput placeholder="Password" secureTextEntry maxLength={64} />
      <TouchableOpacity onPress={onSubmit} accessibilityLabel="Sign in"><Text>Sign in</Text></TouchableOpacity>
    </View>
  );
}
